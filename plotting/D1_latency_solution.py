"""D1 -- order-gateway latency: distribution, comparison, change over time, relationship to load."""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from style import C, DATA, GRAY, hhmm, save

df = pd.read_parquet(DATA / "d1_latency.parquet")
GWS = sorted(df.gateway.unique())

# Q1  What does latency look like?  -> histogram, linear vs log x
p50, p99 = df.latency_us.quantile([0.5, 0.99])
fig, ax = plt.subplots(1, 2, figsize=(11, 3.6))
ax[0].hist(df.latency_us, bins=100, color=C[0])
ax[0].set(title="Linear x: one spike, the tail is invisible", xlabel="latency (us)", ylabel="orders")
ax[1].hist(df.latency_us, bins=np.logspace(0.5, 3, 80), color=C[0])
ax[1].set(xscale="log", title=f"Log x: p50 {p50:.0f} us, p99 {p99:.0f} us", xlabel="latency (us, log)")
for q, name in [(p50, "p50"), (p99, "p99")]:
    ax[1].axvline(q, color="black", lw=0.8)
    ax[1].text(q, ax[1].get_ylim()[1] * 0.92, f" {name}", fontsize=9)
save(fig, "d1_q1_hist.png")

# Q2  Compare gateways  -> box plot (summary) vs ECDF (full shape)
fig, ax = plt.subplots(1, 2, figsize=(11, 3.8))
ax[0].boxplot([df.latency_us[df.gateway == g] for g in GWS], tick_labels=GWS, whis=(1, 99), showfliers=False)
ax[0].set(yscale="log", ylabel="latency (us, log)", title="Box (whiskers p1-p99): gw-3 tail longer")
for g, c in zip(GWS, C):
    x = np.sort(df.latency_us[df.gateway == g].to_numpy())
    ax[1].plot(x, np.arange(1, len(x) + 1) / len(x), color=c, label=g)
ax[1].set(xscale="log", xlabel="latency (us, log)", ylabel="fraction of orders <= x",
          title="ECDF: gw-3 has a 2nd step (~5% slow mode)")
ax[1].legend(loc="lower right")
save(fig, "d1_q2_compare.png")

# Q3  Does anything change during the day?  -> line over time, small multiples p50 / p99, emphasis on gw-2
per5 = df.set_index("ts").groupby("gateway").latency_us.resample("5min").quantile([0.5, 0.99]).unstack()
fig, ax = plt.subplots(2, 1, figsize=(11, 5.5), sharex=True)
for i, (q, label) in enumerate([(0.5, "p50"), (0.99, "p99")]):
    for g in GWS:
        s = per5.loc[g, q]
        ax[i].plot(s.index, s, color=C[0] if g == "gw-2" else GRAY, lw=2 if g == "gw-2" else 1)
    ax[i].set(ylabel=f"{label} latency (us)")
ax[1].annotate("gw-2", xy=(per5.loc["gw-2", 0.99].index[-1], per5.loc["gw-2", 0.99].iloc[-1]), color=C[0])
ax[1].annotate("gw-3 (its slow mode, Q2)", xy=(per5.loc["gw-3", 0.99].index[2], 300), color="dimgray", fontsize=9)
hhmm(ax[1])
ax[0].set_title("gw-2's p99 climbs all afternoon while its median stays flat (grey = other gateways)")
save(fig, "d1_q3_over_time.png")

# Q4  Why is latency high at some times?  -> hexbin (too many points for a scatter) + binned percentiles
bins = pd.cut(df.load_msgs_per_ms, np.arange(20, 140, 5))
g = df.groupby(bins, observed=True).latency_us
binned = g.quantile([0.5, 0.9]).unstack()[g.size() >= 200]        # drop thin bins: their quantiles are noise
mid = [b.mid for b in binned.index]
fig, ax = plt.subplots(figsize=(8, 4.2))
hb = ax.hexbin(df.load_msgs_per_ms, df.latency_us, yscale="log", gridsize=60, cmap="Blues", mincnt=1, bins="log")
ax.plot(mid, binned[0.5], color=C[1], label="median")
ax.plot(mid, binned[0.9], color=C[3], label="p90")
ax.axvline(60, color="black", lw=0.8)
ax.set(xlabel="load (msgs per ms)", ylabel="latency (us, log)",
       title="Flat until ~60 msgs/ms, then latency rises steeply (queueing knee)")
ax.legend(loc="upper left")
fig.colorbar(hb, label="orders (log)")
save(fig, "d1_q4_vs_load.png")
