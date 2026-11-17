"""Generate the data for all five final problems (writes to final5/data/).

Usage:  python3 generate_all.py          # all
        python3 generate_all.py F2 F4    # only some
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

wanted = [a.upper() for a in sys.argv[1:]]
for folder in sorted(HERE.glob("F[0-9]_*")):
    if wanted and folder.name[:2] not in wanted:
        continue
    t0 = time.perf_counter()
    print(f"== {folder.name}")
    subprocess.run([sys.executable, "make_data.py"], cwd=folder, check=True)
    print(f"   done in {time.perf_counter() - t0:.1f}s\n")
