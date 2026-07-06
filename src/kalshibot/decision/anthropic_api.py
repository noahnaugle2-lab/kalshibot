"""Direct Anthropic API runner for trade decisions — the CLI's replacement.

The Claude Code CLI wraps every `claude -p` call in its full harness system
prompt (~19k tokens measured), so per-decision cost via an API key is ~5x a
direct call that sends only our ~3k-token prompt. This runner calls the
Messages API directly with Haiku 4.5, drops the Node/CLI/subprocess
dependency entirely (important for a headless server), and exposes the exact
same interface as ClaudeCLIRunner so DecisionEngine is unchanged.

Auth: ANTHROPIC_API_KEY from .env (the SDK reads it from the environment).
Cost is computed from response usage and returned for the decisions table.
"""

from __future__ import annotations

import logging
import time

from kalshibot.decision.claude_cli import CLIResult, strip_fences

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_TOKENS = 512

# Haiku 4.5 pricing, USD per token (input / output). Cache reads bill ~0.1x.
PRICE_IN = 1.00 / 1_000_000
PRICE_OUT = 5.00 / 1_000_000
PRICE_CACHE_READ = 0.10 / 1_000_000
PRICE_CACHE_WRITE = 1.25 / 1_000_000

SYSTEM_PROMPT = (
    "You are the decision layer of a trading bot for Kalshi 15-minute crypto "
    "binary markets. A deterministic strategy produced a qualifying signal; "
    "decide whether to take it, adjust it, or hold. You cannot exceed the "
    "proposed size. Contracts pay $0.99 if correct, $0 if wrong; prices are in "
    "cents (1-99). Respond with ONLY the JSON object requested — no markdown, "
    "no commentary."
)


class AnthropicRunner:
    """Same surface as ClaudeCLIRunner: async run(prompt) and health_check()."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: str | None = None,
    ) -> None:
        from anthropic import AsyncAnthropic

        self.model = model
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        # AsyncAnthropic reads ANTHROPIC_API_KEY from env when api_key is None;
        # max_retries default 2 handles 429/5xx with backoff.
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s)

    @staticmethod
    def _cost(usage) -> float:
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        return round(
            usage.input_tokens * PRICE_IN
            + usage.output_tokens * PRICE_OUT
            + cache_read * PRICE_CACHE_READ
            + cache_write * PRICE_CACHE_WRITE,
            6,
        )

    async def run(self, prompt: str, timeout_s: float | None = None) -> CLIResult:
        import anthropic

        start = time.monotonic()
        try:
            message = await self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                timeout=timeout_s or self.timeout_s,
            )
        except anthropic.APITimeoutError:
            latency = (time.monotonic() - start) * 1000
            return CLIResult(ok=False, latency_ms=latency,
                             error=f"api timeout after {timeout_s or self.timeout_s}s")
        except anthropic.APIError as exc:
            latency = (time.monotonic() - start) * 1000
            return CLIResult(ok=False, latency_ms=latency, error=f"api error: {exc}")
        latency = (time.monotonic() - start) * 1000
        text = "".join(b.text for b in message.content if b.type == "text")
        return CLIResult(
            ok=True,
            text=strip_fences(text),
            raw_stdout=text,
            latency_ms=latency,
            cost_usd=self._cost(message.usage),
        )

    async def health_check(self, timeout_s: float = 15.0) -> tuple[bool, str]:
        """One tiny call proves the key works and reports latency + model."""
        result = await self.run('Reply with exactly {"ok":true}', timeout_s=timeout_s)
        if not result.ok:
            return False, f"anthropic API call failed: {result.error}"
        return True, f"{self.model}, ping {result.latency_ms:.0f}ms"
