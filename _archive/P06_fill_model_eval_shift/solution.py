"""P06 reference solution -- fill-probability model: evaluation, leakage, shift.

Run:  python3 solution.py        (run make_data.py first)
Prints results for Parts A-F and writes figures to ../plots/p06_*.png
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "p05" / "take_attempts.parquet"
PLOTS = ROOT / "plots"
VENUES = ["EX1", "EX2", "EX3", "EX4", "EX5"]
NUMERIC = ["log_reaction", "edge_ticks", "spread_ticks", "log_qty", "tod_min"]

pd.set_option("display.width", 140)


def section(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


# ----------------------------------------------------------------------------
# metrics (implemented by hand -- no sklearn)
# ----------------------------------------------------------------------------
def log_loss(y, p, eps=1e-12):
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(y, p):
    return float(np.mean((p - y) ** 2))


def auc(y, score):
    """Rank-based AUC = P(score of random positive > score of random negative).

    Equivalent to the Mann-Whitney U statistic / (n_pos * n_neg). Average ranks
    handle ties. O(N log N) because of the sort inside rankdata."""
    y = np.asarray(y).astype(bool)
    ranks = stats.rankdata(score)
    n_pos, n_neg = y.sum(), (~y).sum()
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def ece(y, p, n_bins=10):
    """Expected calibration error with equal-width probability bins."""
    bins = np.minimum((p * n_bins).astype(int), n_bins - 1)
    total = 0.0
    for b in range(n_bins):
        m = bins == b
        if m.any():
            total += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(total)


def scorecard(y, p):
    return {"n": len(y), "base_rate": float(np.mean(y)), "log_loss": log_loss(y, p),
            "brier": brier(y, p), "auc": auc(y, p), "ece": ece(y, p)}


# ----------------------------------------------------------------------------
# logistic regression (IRLS) + feature pipeline
# ----------------------------------------------------------------------------
def fit_logistic(X, y, sample_weight=None, l2=1e-3, iters=60):
    """Weighted logistic regression by Newton/IRLS with a tiny ridge for stability."""
    w_s = np.ones(len(y)) if sample_weight is None else sample_weight
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ beta, -30, 30)))
        w = w_s * p * (1 - p)
        H = X.T @ (X * w[:, None]) + l2 * np.eye(X.shape[1])
        g = X.T @ (w_s * (y - p)) - l2 * beta
        step = np.linalg.solve(H, g)
        beta += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta


def predict_logistic(X, beta):
    return 1 / (1 + np.exp(-np.clip(X @ beta, -30, 30)))


class FillModel:
    """Logistic P(fill) model. Standardization stats are learned on TRAIN only
    (fitting them on all data would leak test-period information)."""

    def __init__(self, numeric=NUMERIC, extra=()):
        self.numeric = list(numeric) + list(extra)

    def _design(self, d):
        z = (d[self.numeric].values - self.mu) / self.sd
        cols = [np.ones(len(d)), z]
        venue = np.column_stack([(d.venue == v).values for v in VENUES[1:]]).astype(float)
        cols.append(venue)
        cols.append((d.strategy == "S2").values.astype(float)[:, None])
        # venue x latency interaction: latency matters more where competition is fast
        lr = z[:, [self.numeric.index("log_reaction")]]
        cols.append(venue * lr)
        return np.column_stack(cols)

    def fit(self, d, sample_weight=None):
        self.mu = d[self.numeric].values.mean(axis=0)
        self.sd = d[self.numeric].values.std(axis=0) + 1e-12
        self.beta = fit_logistic(self._design(d), d.hit.values.astype(float), sample_weight)
        return self

    def predict(self, d):
        return predict_logistic(self._design(d), self.beta)


# ----------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------
def load():
    df = pd.read_parquet(DATA).drop_duplicates()
    df = df[df.reaction_ns.notna() & (df.reaction_ns > 0)].copy()  # need a valid latency
    df["day"] = df["date"].rank(method="dense").astype(int)
    df["hit"] = (df.filled_qty > 0).astype(int)
    df["log_reaction"] = np.log(df.reaction_ns.astype("float64") / 1000.0)
    df["log_qty"] = np.log(df.target_qty)
    # Time of day must be computed in exchange-local time: US DST ended on
    # 2026-11-01, so a UTC-based "minutes since 13:30" jumps by 60 min mid-sample.
    ts = pd.to_datetime(df.ts_ns, unit="ns", utc=True)
    local = ts.dt.tz_convert("America/New_York")
    df["tod_min"] = (local.dt.hour * 60 + local.dt.minute + local.dt.second / 60) - 570
    df["tod_min_utc_naive"] = (ts.dt.hour * 60 + ts.dt.minute + ts.dt.second / 60) - 810
    df["resp_us"] = df.resp_ns / 1000.0
    return df.reset_index(drop=True)


# ----------------------------------------------------------------------------
# Part A -- target, features, leakage
# ----------------------------------------------------------------------------
def part_a(df):
    section("Part A: target, decision-time features, leakage")
    print("target y = 1 if filled_qty > 0 (any fill), else 0")
    print("decision-time features: venue, strategy, edge_ticks, spread_ticks, target_qty,"
          " time of day,\n  our reaction time (known from our own code path / measured "
          "on the way out; see answers.md)")
    print("NOT available at decision time: outcome, filled_qty (the label itself), "
          "resp_ns (exchange response arrives AFTER the outcome)")

    train, test = df[df.day <= 10], df[df.day >= 14]
    print(f"\nAUC of resp_us ALONE on test days 14-20: {auc(test.hit, test.resp_us):.4f}")
    print("mean resp_us by outcome:\n" + df.groupby("outcome").resp_us.mean().round(1).to_string())

    honest = FillModel().fit(train)
    leaky = FillModel(extra=["resp_us"]).fit(train)
    s_h = scorecard(test.hit.values, honest.predict(test))
    s_l = scorecard(test.hit.values, leaky.predict(test))
    print(f"\nmodel WITHOUT resp_us: test AUC {s_h['auc']:.4f}  log loss {s_h['log_loss']:.4f}")
    print(f"model WITH    resp_us: test AUC {s_l['auc']:.4f}  log loss {s_l['log_loss']:.4f}"
          "   <- too good to be true = leak")


# ----------------------------------------------------------------------------
# Part B -- random split vs time split
# ----------------------------------------------------------------------------
def part_b(df):
    section("Part B: random split vs time-based split")
    rng = np.random.default_rng(0)
    is_train = rng.random(len(df)) < 0.7
    m_rand = FillModel().fit(df[is_train])
    s_rand = scorecard(df.hit.values[~is_train], m_rand.predict(df[~is_train]))

    train, test = df[df.day <= 10], df[df.day >= 14]
    m_time = FillModel().fit(train)
    s_time = scorecard(test.hit.values, m_time.predict(test))
    rows = pd.DataFrame({"random 70/30 (all days)": s_rand,
                         "time: train 1-10 -> test 14-20": s_time}).T
    print(rows.round(4).to_string())
    print("-> The random split is optimistic: its training set already contains the\n"
          "   future regime (post-release latencies, post-day-16 EX3), so it never has\n"
          "   to extrapolate. In production we always predict the future from the past,\n"
          "   which is exactly what the time split measures.")


# ----------------------------------------------------------------------------
# Part C -- models + metrics + reliability diagram
# ----------------------------------------------------------------------------
def binned_model(train, test, n_bins=5, alpha=1.0):
    """Lookup table: venue x strategy x latency quintile (edges from TRAIN).
    Laplace smoothing toward the global rate keeps sparse cells sane."""
    edges = np.quantile(train.log_reaction, np.linspace(0, 1, n_bins + 1)[1:-1])
    key = lambda d: (d.venue + "|" + d.strategy + "|"
                     + np.searchsorted(edges, d.log_reaction).astype(str))
    prior = train.hit.mean()
    g = train.groupby(key(train)).hit.agg(["sum", "size"])
    table = (g["sum"] + alpha * prior) / (g["size"] + alpha)
    return key(test).map(table).fillna(prior).values


def part_c(df):
    section("Part C: models on the time split (train 1-10, valid 11-13, test 14-20)")
    train = df[df.day <= 10]
    splits = {"valid 11-13": df[(df.day >= 11) & (df.day <= 13)], "test 14-20": df[df.day >= 14]}
    model = FillModel().fit(train)
    venue_rate = train.groupby("venue").hit.mean()

    results = {}
    for name, d in splits.items():
        y = d.hit.values
        results[(name, "global base rate")] = scorecard(y, np.full(len(d), train.hit.mean()))
        results[(name, "per-venue base rate")] = scorecard(y, d.venue.map(venue_rate).values)
        results[(name, "binned venue x strat x latency")] = scorecard(y, binned_model(train, d))
        results[(name, "logistic")] = scorecard(y, model.predict(d))
    table = pd.DataFrame(results).T
    print(table[["n", "base_rate", "log_loss", "brier", "auc", "ece"]].round(4).to_string())
    print("(AUC of a constant prediction is 0.5 by construction)")

    # reliability diagram on test, split EX3 vs other venues
    test = splits["test 14-20"]
    p = model.predict(test)
    fig, ax = plt.subplots(figsize=(6, 6))
    for label, m in [("other venues", test.venue.values != "EX3"),
                     ("EX3", test.venue.values == "EX3")]:
        bins = np.minimum((p[m] * 10).astype(int), 9)
        dd = pd.DataFrame({"b": bins, "p": p[m], "y": test.hit.values[m]})
        cal = dd.groupby("b").agg(p=("p", "mean"), y=("y", "mean"), n=("y", "size"))
        cal = cal[cal.n >= 30]
        ax.plot(cal.p, cal.y, "o-", label=f"{label} (ECE={ece(test.hit.values[m], p[m]):.3f})")
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect calibration")
    ax.set_xlabel("predicted P(fill)")
    ax.set_ylabel("observed fill rate")
    ax.set_title("Reliability diagram, test days 14-20")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "p06_reliability_test.png", dpi=120)
    plt.close(fig)
    return model


# ----------------------------------------------------------------------------
# Part D -- shift detection
# ----------------------------------------------------------------------------
def psi(expected, actual, edges=None, categorical=False, eps=1e-4):
    """Population Stability Index. Bins come from the reference (expected) sample.
    Rule of thumb: <0.1 stable, 0.1-0.25 moderate, >0.25 major shift."""
    if categorical:
        cats = sorted(set(expected) | set(actual))
        e = pd.Series(expected).value_counts(normalize=True).reindex(cats, fill_value=0).values
        a = pd.Series(actual).value_counts(normalize=True).reindex(cats, fill_value=0).values
    else:
        edges = np.unique(np.quantile(expected, np.linspace(0, 1, 11)[1:-1]))
        e = np.bincount(np.searchsorted(edges, expected), minlength=len(edges) + 1) / len(expected)
        a = np.bincount(np.searchsorted(edges, actual), minlength=len(edges) + 1) / len(actual)
    e, a = e + eps, a + eps
    return float(np.sum((a - e) * np.log(a / e)))


def adversarial_auc(ref, cur, seed=0):
    """Train a classifier to tell 'ref period' rows from 'cur period' rows using the
    model's input features. Held-out AUC ~0.5 => the inputs look the same."""
    both = pd.concat([ref.assign(hit=0), cur.assign(hit=1)], ignore_index=True)
    rng = np.random.default_rng(seed)
    tr = rng.random(len(both)) < 0.7
    m = FillModel().fit(both[tr])          # reuse the same feature pipeline
    score = auc(both.hit.values[~tr], m.predict(both[~tr]))
    coef = pd.Series(m.beta[1:1 + len(NUMERIC)], index=NUMERIC)
    return score, coef


def part_d(df, model):
    section("Part D: shift detection")
    train, test = df[df.day <= 10], df[df.day >= 14]

    rows = []
    for col in NUMERIC + ["tod_min_utc_naive"]:
        ks = stats.ks_2samp(train[col], test[col])
        rows.append({"feature": col, "PSI": psi(train[col].values, test[col].values),
                     "KS stat": ks.statistic})
    for col in ["venue", "strategy"]:
        rows.append({"feature": col, "PSI": psi(train[col].values, test[col].values,
                                                 categorical=True), "KS stat": np.nan})
    print("Covariate shift, train (1-10) vs test (14-20):")
    print(pd.DataFrame(rows).set_index("feature").round(4).to_string())
    print("-> log_reaction shifted: S1 got ~300 ns faster on day 11 (known release).\n"
          "   tod_min_utc_naive 'shifts' only because DST ended Nov 1 -- an artifact of\n"
          "   computing time-of-day in UTC instead of exchange-local time.")
    s1 = df[df.strategy == "S1"]
    print("   S1 venue mix days 1-10 vs 14-20 PSI: "
          f"{psi(s1[s1.day <= 10].venue.values, s1[s1.day >= 14].venue.values, categorical=True):.3f}")

    auc_a, coef_a = adversarial_auc(train, test)
    print(f"\nAdversarial validation  days 1-10 vs 14-20: AUC = {auc_a:.3f}")
    print("  standardized coefficients:", coef_a.round(2).to_dict())
    pre, post = df[(df.day >= 11) & (df.day <= 15)], df[df.day >= 16]
    auc_b, _ = adversarial_auc(pre, post)
    auc_c, _ = adversarial_auc(pre[pre.venue == "EX3"], post[post.venue == "EX3"])
    print(f"Adversarial validation  days 11-15 vs 16-20: AUC = {auc_b:.3f}  "
          f"(EX3 rows only: {auc_c:.3f})  -> inputs did NOT shift")

    # performance by day x venue: calibration gap = mean(pred) - mean(actual)
    d = df.copy()
    d["pred"] = model.predict(d)
    gap = d.groupby(["day", "venue"]).apply(
        lambda g: g.pred.mean() - g.hit.mean(), include_groups=False).unstack()
    daily_ll = d.groupby("day").apply(lambda g: log_loss(g.hit.values, g.pred.values),
                                      include_groups=False)
    print("\nCalibration gap (mean predicted - observed) by day x venue:")
    print(gap.round(3).to_string())
    ex3_before = gap.loc[11:15, "EX3"].mean()
    ex3_after = gap.loc[16:20, "EX3"].mean()
    print(f"-> EX3 gap days 11-15: {ex3_before:+.3f}; days 16-20: {ex3_after:+.3f}. "
          "Inputs unchanged, P(y|x) changed => CONCEPT DRIFT on EX3 from day 16.")

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    for v in VENUES:
        axes[0].plot(gap.index, gap[v], marker="o", lw=2.5 if v == "EX3" else 1, label=v)
    axes[0].axhline(0, color="k", lw=0.8)
    axes[0].axvspan(0.5, 10.5, color="gray", alpha=0.1, label="training days")
    axes[0].set_ylabel("mean pred - observed")
    axes[0].set_title("Calibration gap by day and venue (model trained on days 1-10)")
    axes[0].legend(ncol=6, fontsize=8)
    axes[1].plot(daily_ll.index, daily_ll.values, marker="o")
    axes[1].set_ylabel("daily log loss")
    axes[1].set_xlabel("trading day")
    axes[1].set_xticks(range(1, 21))
    fig.tight_layout()
    fig.savefig(PLOTS / "p06_calibration_gap_by_day.png", dpi=120)
    plt.close(fig)


# ----------------------------------------------------------------------------
# Part E -- remedies
# ----------------------------------------------------------------------------
def platt_per_venue(base_model, calib):
    """Per-venue Platt scaling: logit(p') = a * logit(p) + b, fitted on recent labels."""
    params = {}
    for v in VENUES:
        c = calib[calib.venue == v]
        p = np.clip(base_model.predict(c), 1e-6, 1 - 1e-6)
        X = np.column_stack([np.ones(len(c)), np.log(p / (1 - p))])
        params[v] = fit_logistic(X, c.hit.values.astype(float), l2=1e-6)

    def predict(d):
        p = np.clip(base_model.predict(d), 1e-6, 1 - 1e-6)
        z = np.log(p / (1 - p))
        out = np.empty(len(d))
        for v in VENUES:
            m = (d.venue == v).values
            a = params[v]
            out[m] = 1 / (1 + np.exp(-(a[0] + a[1] * z[m])))
        return out

    return predict, params


def part_e(df):
    section("Part E: remedies, evaluated on days 18-20 (labels for 16-17 assumed available)")
    eval_d = df[df.day >= 18]
    static = FillModel().fit(df[df.day <= 10])
    platt_pred, platt_params = platt_per_venue(static, df[(df.day >= 16) & (df.day <= 17)])
    rolling = FillModel().fit(df[(df.day >= 13) & (df.day <= 17)])
    hist = df[df.day <= 17]
    half_life_days = 2.0
    weights = 0.5 ** ((17 - hist.day.values) / half_life_days)
    recency = FillModel().fit(hist, sample_weight=weights)

    candidates = {
        "static (train 1-10)": static.predict,
        "static + per-venue Platt (16-17)": platt_pred,
        "rolling retrain (13-17)": rolling.predict,
        "recency-weighted (1-17, half-life 2d)": recency.predict,
    }
    rows = {}
    for name, f in candidates.items():
        p = f(eval_d)
        y = eval_d.hit.values
        ex3 = (eval_d.venue == "EX3").values
        s_all, s_ex3 = scorecard(y, p), scorecard(y[ex3], p[ex3])
        rows[name] = {"log_loss": s_all["log_loss"], "ece": s_all["ece"], "auc": s_all["auc"],
                      "EX3 log_loss": s_ex3["log_loss"], "EX3 ece": s_ex3["ece"],
                      "EX3 mean pred": float(p[ex3].mean()), "EX3 actual": float(y[ex3].mean())}
    print(pd.DataFrame(rows).T.round(4).to_string())
    a, b = platt_params["EX3"][1], platt_params["EX3"][0]
    print(f"EX3 Platt params: slope {a:.2f}, intercept {b:+.2f} (pulls EX3 predictions down)")


# ----------------------------------------------------------------------------
# Part F -- message to traders
# ----------------------------------------------------------------------------
def part_f(df, model):
    section("Part F: what traders can / cannot trust")
    test = df[df.day >= 14]
    p = model.predict(test)
    ok = (test.venue != "EX3").values
    post_ex3 = (test.venue == "EX3").values & (test.day >= 16).values
    print(
        f"* On venues whose competition did not change (all but EX3) the model ranks\n"
        f"  opportunities reasonably (test AUC {auc(test.hit.values[ok], p[ok]):.2f}) and is "
        f"calibrated within ~{ece(test.hit.values[ok], p[ok]) * 100:.0f} pp,\n"
        "  so it is usable for the 'send IOC iff P(fill) * edge > cost' decision there.\n"
        f"* It is NOT trustworthy on EX3 since Nov 9: it predicts {p[post_ex3].mean():.2f} "
        f"but we fill {test.hit.values[post_ex3].mean():.2f}.\n"
        "  Our inputs didn't change -- the world did (a faster competitor) -- so input\n"
        "  monitoring alone would never have caught it; outcome monitoring did.\n"
        "* Fix: per-venue recalibration on the latest labelled days + rolling retrain, and a\n"
        "  daily calibration-by-venue alert. Until recalibrated, treat EX3 predictions as\n"
        "  upper bounds and raise the edge threshold there.\n"
        "* The model is observational: it predicts fills at OUR current latency and the\n"
        "  current competition; it is not a causal estimate of what latency is worth.")


def main():
    PLOTS.mkdir(parents=True, exist_ok=True)
    df = load()
    print(f"loaded {len(df):,} attempts with valid latency over {df.day.nunique()} days")
    part_a(df)
    part_b(df)
    model = part_c(df)
    part_d(df, model)
    part_e(df)
    part_f(df, model)


if __name__ == "__main__":
    main()
