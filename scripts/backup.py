"""Create and verify an atomic SQLite backup, then optionally copy it offsite."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshibot.persistence.backup import _remove_sqlite_temp, backup_database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "data/kalshibot.db")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/backups")
    # The 75 GB production host has room for one previous local backup plus
    # the next atomic temporary copy. Longer retention belongs off-host.
    parser.add_argument("--keep", type=int, default=1)
    parser.add_argument("--verify-only", type=Path)
    args = parser.parse_args()

    if args.verify_only:
        with sqlite3.connect(args.verify_only) as db:
            result = db.execute("PRAGMA quick_check(1)").fetchone()[0]
        print(result)
        return 0 if result == "ok" else 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.output_dir / ".backup.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another backup is already running", file=sys.stderr)
            return 1

        # The process lock proves these are leftovers, not active writes.
        stale_bases = {
            Path(str(stale).split(".tmp", 1)[0] + ".tmp")
            for stale in args.output_dir.glob("kalshibot-*.tmp*")
        }
        for stale in stale_bases:
            _remove_sqlite_temp(stale)

        source_bytes = args.source.stat().st_size
        free_bytes = shutil.disk_usage(args.output_dir).free
        safety_bytes = max(1 << 30, source_bytes // 10)
        required_bytes = source_bytes + safety_bytes
        if free_bytes < required_bytes:
            print(
                "insufficient backup space: "
                f"need {required_bytes} bytes, have {free_bytes} bytes",
                file=sys.stderr,
            )
            return 1

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = args.output_dir / f"kalshibot-{stamp}.db"
        backup_database(args.source, destination)

        with sqlite3.connect(args.source) as db:
            db.execute(
                "INSERT INTO meta(key,value) VALUES('last_backup_ts',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),)
            )
            db.commit()

        backups = sorted(args.output_dir.glob("kalshibot-*.db"), reverse=True)
        for old in backups[max(1, args.keep):]:
            old.unlink()

        remote = os.getenv("BACKUP_RCLONE_DEST")
        if remote:
            subprocess.run(["rclone", "copy", str(destination), remote], check=True)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
