"""Archive aged tape rows and prune the hot SQLite database.

The scheduled service uses ``--no-vacuum`` so it can run alongside the
recorder without taking a long exclusive database lock. Operators can run a
controlled maintenance-window compaction by omitting that flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.config import PROJECT_ROOT, Settings
from kalshibot.persistence.db import Database
from kalshibot.persistence.retention import run_retention


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data/kalshibot.db")
    parser.add_argument("--archive-dir", type=Path, default=PROJECT_ROOT / "data/archive")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--no-vacuum", action="store_true")
    args = parser.parse_args()

    days = args.days if args.days is not None else Settings().retention_days
    if not 1 <= days <= 90:
        parser.error("--days must be between 1 and 90")

    db = Database(args.db)
    try:
        result = run_retention(
            db,
            retention_days=days,
            archive_dir=args.archive_dir,
            vacuum=not args.no_vacuum,
        )
    finally:
        db.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
