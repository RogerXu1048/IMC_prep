"""P06 uses the same dataset as P05 (IOC take attempts).

Usage:  python3 make_data.py
If ../data/p05/take_attempts.parquet is missing, this runs P05's generator.
"""
import runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data" / "p05" / "take_attempts.parquet"
P05_GENERATOR = HERE.parent / "P05_ioc_hit_rate" / "make_data.py"


def main():
    if DATA.exists():
        print(f"found {DATA}  ({DATA.stat().st_size / 1e6:.1f} MB) -- nothing to do")
        return
    print("P05 data not found; running P05 generator ...")
    runpy.run_path(str(P05_GENERATOR), run_name="__main__")


if __name__ == "__main__":
    main()
