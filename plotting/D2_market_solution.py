"""D2 -- market data over the day: price path, a zoom on an event, spread distribution, intraday profile, day x time."""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from style import C, DATA, GRAY, hhmm, save

q = pd.read_parquet(DATA / "d2_quotes_1s.parquet").set_index("ts")
q["mid"] = (q.bid + q.ask) / 2
q["spread_ticks"] = ((q.ask - q.bid) / 0.01).round().astype(int)
days = pd.read_parquet(DATA / "d2_intraday_20d.parquet")
NEWS = pd.Timestamp("2026-11-12 10:00:00")

# Q1  How did the price move today?  -> line of the 1-minute mid (1-second data is too dense for a whole day)
m1 = q.mid.resample("1min").last()
fig, ax = plt.subplots(figsize=(10, 3.6))
ax.plot(m1.index, m1, color=C[0])
ax.axvline(NEWS, color="black", lw=0.8)
ax.annotate("10:00 news: -1.5% in ~20 s", xy=(NEWS, m1.loc[NEWS]), xytext=(10, 15), textcoords="offset points")
ax.set(ylabel="mid price ($)", title="Price gapped down 1.5% at 10:00, then won back about half by the close")
hhmm(ax)
save(fig, "d2_q1_price.png")

# Q2  What happened around 10:00?  -> step plot of bid/ask (quotes are piecewise constant) + volume bars on a
#     SEPARATE panel with a shared x axis (no dual y axis)
w = q.loc["2026-11-12 09:59:30":"2026-11-12 10:01:00"]
fig, ax = plt.subplots(2, 1, figsize=(10, 5), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
ax[0].step(w.index, w.ask, where="post", color=C[1], label="ask")
ax[0].step(w.index, w.bid, where="post", color=C[0], label="bid")
ax[0].legend(loc="upper right")
ax[0].set(ylabel="price ($)", title="On the news the spread blew out to 5-10 ticks for ~40 s while volume spiked")
ax[1].bar(w.index, w.volume, width=1 / 86400, color=GRAY)
ax[1].set(ylabel="shares / s")
save(fig, "d2_q2_zoom.png")

# Q3  How wide is the spread normally?  -> bar of counts per tick value (spread is discrete, not continuous)
normal = q.loc["2026-11-12 09:40":"2026-11-12 15:59"].drop(q.loc[NEWS:NEWS + pd.Timedelta("2min")].index)
share = normal.spread_ticks.value_counts(normalize=True).sort_index()
fig, ax = plt.subplots(figsize=(6, 3.4))
ax.bar(share.index, share * 100, color=C[0], width=0.6)
ax.set(xticks=share.index, xlabel="spread (ticks)", ylabel="% of seconds",
       title=f"Normal hours: 1 tick {share.loc[1]:.0%} of the time")
save(fig, "d2_q3_spread.png")

# Q4  What does a typical day's volume look like?  -> median by minute across 20 days + IQR band
prof = days.groupby("minute").volume.quantile([0.25, 0.5, 0.75]).unstack() / 1e3
fig, ax = plt.subplots(figsize=(10, 3.6))
ax.fill_between(prof.index, prof[0.25], prof[0.75], color=C[0], alpha=0.2, lw=0, label="25-75% of days")
ax.plot(prof.index, prof[0.5], color=C[0], label="median day")
ax.set(xlabel="minutes since 09:30", ylabel="volume (k shares / min)",
       title="Volume is U-shaped: the open and close trade 3-5x midday")
ax.legend()
save(fig, "d2_q4_profile.png")

# Q5  Is the spread pattern the same every day?  -> heatmap: day x 15-minute bucket, sequential colour
days["bucket"] = days.minute // 15 * 15
grid = days.pivot_table(index="date", columns="bucket", values="spread_bps", aggfunc="median")
fig, ax = plt.subplots(figsize=(10, 4.4))
im = ax.imshow(grid, aspect="auto", cmap="Blues", interpolation="nearest")
ax.set_xticks(range(0, grid.shape[1], 4), [f"{9 + (30 + b) // 60}:{(30 + b) % 60:02d}" for b in grid.columns[::4]])
ax.set_yticks(range(grid.shape[0]), [d.strftime("%m-%d") for d in grid.index], fontsize=7)
ax.grid(False)
ax.set(xlabel="time of day (ET)", title="Spreads are widest in the first 15 min every day; one news day stands out")
fig.colorbar(im, label="median spread (bps)")
save(fig, "d2_q5_heatmap.png")
