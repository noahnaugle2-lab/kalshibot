"""Consistent, integrity-checked SQLite backup primitives."""

from pathlib import Path
import sqlite3


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(tmp) as dst:
        src.backup(dst)
        # quick_check validates every page and core B-tree consistency once.
        # Full integrity_check revalidates every index and can saturate the VPS
        # disk for many minutes on the multi-GB tape database.
        result = dst.execute("PRAGMA quick_check(1)").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"backup integrity check failed: {result}")
    tmp.replace(destination)
