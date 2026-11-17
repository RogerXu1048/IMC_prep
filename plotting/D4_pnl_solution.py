"""D4 -- strategy PnL review: cumulative performance, risk, attribution, diversification, a before/after."""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from style import C, DATA, GRAY, save

daily = pd.read_parquet(DATA / "d4_pnl_daily.parquet").pivot(index="date", columns="strategy", values="pnl")
by_sym = pd.read_parquet(DATA / "d4_pnl_by_symbol.parquet")
DEPLOY = daily.index[80]                                   # S4 v2 went live on this day

# Q1  How did each strategy do?  -> cumulative PnL lines (daily bars x 4 strategies would be unreadable)
cum = daily.cumsum() / 1e6
fig, ax = plt.subplots(figsize=(10, 4))
for s, c in zip(cum.columns, C):
    ax.plot(cum.index, cum[s], color=c)
    ax.annotate(f"{s} {cum[s].iloc[-1]:+.1f}M", (cum.index[-1], cum[s].iloc[-1]), xytext=(4, 0),
                textcoords="offset points", va="center", color=c, fontsize=9)
ax.axvline(DEPLOY, color="black", lw=0.8)
ax.annotate("S4 v2 deployed", (DEPLOY, ax.get_ylim()[1]), xytext=(4, -12), textcoords="offset points", fontsize=9)
ax.axhline(0, color="black", lw=0.6)
ax.set(ylabel="cumulative PnL ($M)", title="S1/S2 grind up; S3 swings wildly and ends negative; S4 went flat after its deploy")
save(fig, "d4_q1_cumulative.png")

# Q2  How risky is S3?  -> histogram of daily PnL (fat tail) + drawdown ("underwater") chart
s3 = daily.S3 / 1e3
dd = (s3.cumsum() - s3.cumsum().cummax())
fig, ax = plt.subplots(1, 2, figsize=(11, 3.6))
ax[0].hist(s3, bins=40, color=C[0])
ax[0].axvline(0, color="black", lw=0.8)
ax[0].annotate(f"worst day {s3.min():,.0f}k", (s3.min(), 1), xytext=(10, 25), textcoords="offset points",
               arrowprops={"arrowstyle": "->"}, fontsize=9)
ax[0].set(xlabel="S3 daily PnL ($k)", ylabel="days",
          title=f"Fat tails: worst {s3.min():,.0f}k, best {s3.max():+,.0f}k vs typical +-{s3.std():,.0f}k")
ax[1].fill_between(dd.index, dd, 0, color=C[1], alpha=0.6, lw=0)
ax[1].set(ylabel="drawdown from peak ($k)", title=f"Max drawdown {dd.min():,.0f}k; never recovered")
ax[1].tick_params(axis="x", rotation=30)
save(fig, "d4_q2_s3_risk.png")

# Q3  Where does S1's PnL come from?  -> sorted horizontal bars, top 12 + "Other", losses in a second colour
top = by_sym.sort_values("pnl", key=abs, ascending=False)
show = pd.concat([top.head(12), pd.DataFrame({"symbol": [f"Other ({len(top) - 12})"], "pnl": [top.pnl.iloc[12:].sum()]})])
show = show.iloc[::-1]
top5 = by_sym.nlargest(5, "pnl").pnl.sum() / by_sym.pnl.sum()
fig, ax = plt.subplots(figsize=(7, 4.4))
ax.barh(show.symbol, show.pnl / 1e3, color=[C[0] if v >= 0 else C[1] for v in show.pnl])
ax.axvline(0, color="black", lw=0.6)
ax.set(xlabel="S1 PnL ($k)", title=f"Top 5 symbols make {top5:.0%} of S1's PnL")
save(fig, "d4_q3_attribution.png")

# Q4  Do the strategies diversify each other?  -> correlation heatmap, diverging colours centred on 0
corr = daily.corr()
fig, ax = plt.subplots(figsize=(4.6, 3.8))
im = ax.imshow(corr, cmap="RdBu_r", vmin=-1, vmax=1)
ax.set_xticks(range(4), corr.columns)
ax.set_yticks(range(4), corr.index)
ax.grid(False)
for i in range(4):
    for j in range(4):
        ax.text(j, i, f"{corr.iat[i, j]:.2f}", ha="center", va="center", fontsize=9,
                color="white" if abs(corr.iat[i, j]) > 0.6 else "black")
ax.set_title(f"S1 and S2 move together (rho={corr.loc['S1', 'S2']:.2f})")
fig.colorbar(im)
save(fig, "d4_q4_correlation.png")

# Q5  Did the S4 deploy hurt?  -> daily bars + rolling mean over time, and before/after box plots with n
s4 = daily.S4 / 1e3
before, after = s4[s4.index < DEPLOY], s4[s4.index >= DEPLOY]
fig, ax = plt.subplots(1, 2, figsize=(11, 3.6), gridspec_kw={"width_ratios": [2.2, 1]})
ax[0].bar(s4.index, s4, color=GRAY, width=1)
ax[0].plot(s4.rolling(10).mean(), color=C[0], label="10-day mean")
ax[0].axvline(DEPLOY, color="black", lw=0.8)
ax[0].set(ylabel="S4 daily PnL ($k)", title="After the deploy the 10-day mean drops to ~0")
ax[0].legend(loc="lower left")
ax[0].tick_params(axis="x", rotation=30)
ax[1].boxplot([before, after], tick_labels=[f"before\nn={len(before)}", f"after\nn={len(after)}"])
ax[1].axhline(0, color="black", lw=0.6)
ax[1].set(title=f"Mean {before.mean():+.0f}k -> {after.mean():+.0f}k / day")
save(fig, "d4_q5_before_after.png")
