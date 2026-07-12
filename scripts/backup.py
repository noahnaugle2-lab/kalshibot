"""Create and verify an atomic SQLite backup, then optionally copy it offsite."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshibot.persistence.backup import backup_database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "data/kalshibot.db")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/backups")
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--verify-only", type=Path)
    args = parser.parse_args()

    if args.verify_only:
        with sqlite3.connect(args.verify_only) as db:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
        print(result)
        return 0 if result == "ok" else 1

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
