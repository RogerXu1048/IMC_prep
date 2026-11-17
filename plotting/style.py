"""Shared paths + a quiet matplotlib style for the plotting drills (not something you write in an interview)."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from cycler import cycler

HERE = Path(__file__).resolve().parent
DATA, PLOTS = HERE / "data", HERE / "plots"
PLOTS.mkdir(exist_ok=True)

C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]   # fixed categorical order
GRAY = "#b4b3ad"                                             # de-emphasis

plt.rcParams.update({
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "axes.axisbelow": True, "grid.color": "#e8e7e3", "grid.linewidth": 0.6,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "lines.linewidth": 1.6, "figure.dpi": 110, "axes.prop_cycle": cycler(color=C),
})


def save(fig, name: str) -> None:
    fig.tight_layout()
    fig.savefig(PLOTS / name)
    plt.close(fig)
    print("saved", name)


def hhmm(ax) -> None:
    """Label a datetime x axis as HH:MM (one trading day does not need the date on every tick)."""
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
