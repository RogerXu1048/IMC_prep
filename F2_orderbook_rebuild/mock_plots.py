"""F2 mock: two extra charts for the presentation part (run after make_data.py).

  f2_feed_health.png    feed message rate per second + the window where our book was stale
  f2_spread_dist.png    spread distribution weighted by events vs by time
"""
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from solution import DATA, PLOTS, step1_load_and_audit, step2_rebuild, step3_stats

PLOTS.mkdir(parents=True, exist_ok=True)
msgs = step1_load_and_audit(DATA / "feed.jsonl")
bbo, trades = step2_rebuild(msgs)
live = step3_stats(bbo, trades)                      # adds dur_s = how long each BBO state lasted

# 1) Feed health: is the data trustworthy over time?  -> line over time + shaded stale window
ts = pd.to_datetime(pd.Series([m["ts"] for m in msgs if "ts" in m]), unit="ns", utc=True)
rate = ts.dt.floor("1s").value_counts().sort_index()
stale = bbo[bbo.stale]
fig, ax = plt.subplots(figsize=(11, 3.6))
ax.plot(rate.index, rate.to_numpy(), lw=0.8, color="#2a78d6")
ax.axvspan(stale.ts.min(), stale.ts.max(), color="#eb6834", alpha=0.25, lw=0, label="book stale (gap -> wait for snapshot)")
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
ax.set(xlabel="time (UTC)", ylabel="messages / second", title="Feed is bursty; after the seq gap our book was marked stale until the next snapshot")
ax.legend(loc="upper right")
fig.savefig(PLOTS / "f2_feed_health.png", dpi=110, bbox_inches="tight")
plt.close(fig)

# 2) Spread: per event vs per unit of time  -> grouped bars per tick value (spread is discrete)
spread = (live.ask - live.bid).astype(int).clip(upper=5)      # 5 = "5+"
by_event = spread.value_counts(normalize=True).sort_index()
by_time = live.dur_s.groupby(spread).sum() / live.dur_s.sum()
x = np.arange(len(by_event))
fig, ax = plt.subplots(figsize=(7, 3.8))
ax.bar(x - 0.2, by_event * 100, 0.4, label="% of book updates", color="#b4b3ad")
ax.bar(x + 0.2, by_time.reindex(by_event.index) * 100, 0.4, label="% of time", color="#2a78d6")
ax.set_xticks(x, [f"{t}" if t < 5 else "5+" for t in by_event.index])
ax.set(xlabel="spread (ticks)", ylabel="%",
       title="Wide spreads are common in events but short-lived in time")
ax.legend()
fig.savefig(PLOTS / "f2_spread_dist.png", dpi=110, bbox_inches="tight")
plt.close(fig)
print("saved f2_feed_health.png, f2_spread_dist.png")
