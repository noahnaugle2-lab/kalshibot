"""Async runner for the Claude Code CLI in headless mode.

Measured reality (2026-07-04, CLI 2.1.170): a trivial prompt takes 4-12s
wall clock depending on model and cache state, and output arrives inside a
JSON envelope whose `.result` field holds the model's text — often wrapped
in markdown fences. Latency is therefore a first-class constraint handled
upstream (async dispatch, staleness discard); this module's jobs are:

- a worker pool (semaphore) capping concurrent CLI processes
- hard timeout with process kill on expiry
- envelope parsing + fence stripping
- a startup health check (version + one tiny authenticated call) so AI mode
  refuses to enable when the CLI is missing or unauthenticated
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_CONCURRENT = 2
FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)


@dataclass
class CLIResult:
    ok: bool
    text: str | None = None          # inner .result text, fences stripped
    raw_stdout: str = ""
    latency_ms: float = 0.0
    error: str | None = None
    cost_usd: float | None = None


def strip_fences(text: str) -> str:
    return FENCE_RE.sub("", text.strip()).strip()


class ClaudeCLIRunner:
    def __init__(
        self,
        model: str | None = "haiku",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        binary: str = "claude",
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.binary = binary
        self._sem = asyncio.Semaphore(max_concurrent)

    def _args(self) -> list[str]:
        args = [self.binary, "-p", "--output-format", "json"]
        if self.model:
            args += ["--model", self.model]
        return args

    async def run(self, prompt: str, timeout_s: float | None = None) -> CLIResult:
        """One headless CLI call: prompt via stdin, killed hard on timeout."""
        timeout = timeout_s if timeout_s is not None else self.timeout_s
        start = time.monotonic()
        async with self._sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._args(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                return CLIResult(ok=False, error="claude CLI not found")
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                latency = (time.monotonic() - start) * 1000
                return CLIResult(ok=False, latency_ms=latency,
                                 error=f"timeout after {timeout}s (killed)")
        latency = (time.monotonic() - start) * 1000
        raw = stdout.decode(errors="replace")
        if proc.returncode != 0:
            return CLIResult(ok=False, raw_stdout=raw, latency_ms=latency,
                             error=f"exit {proc.returncode}: {stderr.decode(errors='replace')[:300]}")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            return CLIResult(ok=False, raw_stdout=raw, latency_ms=latency,
                             error=f"envelope parse: {exc}")
        if envelope.get("is_error"):
            return CLIResult(ok=False, raw_stdout=raw, latency_ms=latency,
                             error=f"CLI error: {str(envelope.get('result'))[:300]}")
        return CLIResult(
            ok=True,
            text=strip_fences(str(envelope.get("result") or "")),
            raw_stdout=raw,
            latency_ms=latency,
            cost_usd=envelope.get("total_cost_usd"),
        )

    async def health_check(self, timeout_s: float = 45.0) -> tuple[bool, str]:
        """Version check + one tiny authenticated call. Slow; run at startup only."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = out.decode().strip()
        except (FileNotFoundError, asyncio.TimeoutError) as exc:
            return False, f"claude CLI unavailable: {exc}"
        result = await self.run('Reply with exactly {"ok":true}', timeout_s=timeout_s)
        if not result.ok:
            return False, f"CLI call failed ({version}): {result.error}"
        return True, f"{version}, ping {result.latency_ms:.0f}ms"
