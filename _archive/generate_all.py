"""Generate the datasets for every practice problem (P05 must run before P06).

Usage:  python3 generate_all.py            # all problems
        python3 generate_all.py P03 P10    # only some
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    wanted = [a.upper() for a in sys.argv[1:]]
    folders = sorted(p for p in HERE.glob("P[0-9][0-9]_*") if (p / "make_data.py").exists())
    for folder in folders:
        if wanted and folder.name[:3] not in wanted:
            continue
        t0 = time.perf_counter()
        print(f"== {folder.name}")
        subprocess.run([sys.executable, "make_data.py"], cwd=folder, check=True)
        print(f"   done in {time.perf_counter() - t0:.1f}s\n")


if __name__ == "__main__":
    main()
