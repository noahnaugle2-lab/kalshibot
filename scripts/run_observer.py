"""Run observation mode: record market data for all assets, trade nothing.

Usage:
    python scripts/run_observer.py                 # foreground
    nohup python scripts/run_observer.py >> logs/observer.log 2>&1 &
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalshibot.observer import main

if __name__ == "__main__":
    main()
