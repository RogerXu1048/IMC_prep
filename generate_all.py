"""Generate data for the five kept problems and the plotting drills.

Usage:  python3 generate_all.py          # everything
        python3 generate_all.py F2 F3    # only some
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
wanted = [a.upper() for a in sys.argv[1:]]
for folder in sorted(HERE.glob("F[0-9]_*")) + [HERE / "plotting"]:
    if wanted and folder.name[:2].upper() not in wanted and folder.name.upper() not in wanted:
        continue
    t0 = time.perf_counter()
    print(f"== {folder.name}")
    subprocess.run([sys.executable, "make_data.py"], cwd=folder, check=True, stdout=subprocess.DEVNULL)
    print(f"   done in {time.perf_counter() - t0:.1f}s")
