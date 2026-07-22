"""Consistent, integrity-checked SQLite backup primitives."""

from pathlib import Path
import sqlite3


def _remove_sqlite_temp(path: Path) -> None:
    """Remove a temporary database and SQLite sidecars from a failed run."""
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        candidate.unlink(missing_ok=True)


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".tmp")
    _remove_sqlite_temp(tmp)
    try:
        with sqlite3.connect(source) as src, sqlite3.connect(tmp) as dst:
            src.backup(dst)
            # quick_check validates every page and core B-tree consistency once.
            # Full integrity_check revalidates every index and can saturate the VPS
            # disk for many minutes on the multi-GB tape database.
            result = dst.execute("PRAGMA quick_check(1)").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"backup integrity check failed: {result}")
        tmp.replace(destination)
        _remove_sqlite_temp(tmp)
    except BaseException:
        # A full or interrupted disk must not strand a source-sized temporary
        # file and prevent the next recovery attempt from running.
        _remove_sqlite_temp(tmp)
        raise
