"""D3 -- execution quality by venue: categorical comparison with uncertainty, relationship, 2-D pattern, composition."""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from style import C, DATA, GRAY, save

df = pd.read_parquet(DATA / "d3_orders.parquet")
VENUES = sorted(df.venue.unique())
COLOR = dict(zip(VENUES, C))                    # colour follows the venue in every chart

# Q1  Which venue fills best?  -> sorted dot plot with 95% CI and n (a zoomed bar chart would exaggerate gaps)
r = df.groupby("venue").filled.agg(rate="mean", n="size").sort_values("rate")
r["ci"] = 1.96 * np.sqrt(r.rate * (1 - r.rate) / r.n)
fig, ax = plt.subplots(figsize=(7, 3.4))
ax.errorbar(r.rate * 100, r.index, xerr=r.ci * 100, fmt="o", color=C[0], ecolor=GRAY, capsize=3)
for v, row in r.iterrows():
    ax.annotate(f"n={row.n:,.0f}", (row.rate * 100, v), xytext=(0, 7), textcoords="offset points",
                ha="center", fontsize=8, color="dimgray")
ax.set(xlabel="fill rate (%) with 95% CI",
       title="A fills best; E looks worst but with n=169 its CI overlaps D")
save(fig, "d3_q1_fill_rate.png")

# Q2  Does order size affect slippage?  -> scatter (sampled, transparent) + binned median and p90
f = df[df.filled]
bins = pd.qcut(f.order_size, 12, duplicates="drop")
b = f.groupby(bins, observed=True).agg(size=("order_size", "median"), med=("slippage_bps", "median"),
                                       p90=("slippage_bps", lambda s: s.quantile(0.9)))
s = f.sample(5000, random_state=0)
fig, ax = plt.subplots(figsize=(8, 4))
ax.scatter(s.order_size, s.slippage_bps, s=6, alpha=0.15, color=GRAY, label="orders (5k sample)")
ax.plot(b["size"], b.med, color=C[0], marker="o", label="median per size bucket")
ax.plot(b["size"], b.p90, color=C[1], marker="o", label="p90 per size bucket")
ax.set(xscale="log", ylim=(-4, 12), xlabel="order size (shares, log)", ylabel="slippage (bps)",
       title="Slippage grows with order size (~ square root), and so does its spread")
ax.legend(loc="upper left")
save(fig, "d3_q2_slippage_vs_size.png")

# Q3  When in the day does each venue struggle?  -> heatmap venue x 30-min bucket of MISS rate (darker = worse)
df["bucket"] = (df.ts - df.ts.dt.normalize() - pd.Timedelta("09:30:00")) // pd.Timedelta("30min")
grid = 1 - df[df.venue != "E"].pivot_table(index="venue", columns="bucket", values="filled", aggfunc="mean")
fig, ax = plt.subplots(figsize=(10, 2.8))
im = ax.imshow(grid * 100, aspect="auto", cmap="Blues", interpolation="nearest")
ax.set_xticks(range(grid.shape[1]), [f"{9 + (30 + 30 * b) // 60}:{(30 + 30 * b) % 60:02d}" for b in grid.columns])
ax.set_yticks(range(grid.shape[0]), grid.index)
ax.grid(False)
ax.set(xlabel="30-min bucket starting (ET)", title="D misses ~38% of orders in the first 30 min; fine afterwards (E omitted: too few orders)")
fig.colorbar(im, label="miss rate (%)")
save(fig, "d3_q3_heatmap.png")

# Q4  How has our routing mix changed?  -> 100% stacked area of daily volume share by venue
share = df.pivot_table(index="date", columns="venue", values="order_size", aggfunc="sum")
share = share.div(share.sum(axis=1), axis=0) * 100
fig, ax = plt.subplots(figsize=(10, 3.8))
ax.stackplot(share.index, share.T.to_numpy(), colors=[COLOR[v] for v in share.columns], labels=share.columns,
             edgecolor="white", linewidth=0.5)
ax.set(ylim=(0, 100), ylabel="% of routed shares", title="C's share roughly doubled (~15% -> ~35%), mostly taken from A")
ax.legend(loc="upper left", bbox_to_anchor=(1, 1), title="venue")
save(fig, "d3_q4_mix.png")
