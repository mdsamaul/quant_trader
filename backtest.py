import math
import numpy as np
import pandas as pd
from scipy import stats


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (centre - adj) / d


def simulate(prob: pd.Series, y: pd.Series, threshold: float, expiry: int, payout: float, stake: float = 1.0):
    """One trade at a time (a new trade only after the previous one expired).
    CALL if p >= thr, PUT if p <= 1-thr. Ties (y NaN) are refunded."""
    p, yy = prob.values, y.values
    trades, next_ok = [], 0
    for i in range(len(p)):
        if i < next_ok or np.isnan(p[i]):
            continue
        if p[i] >= threshold:
            d = 1
        elif p[i] <= 1 - threshold:
            d = 0
        else:
            continue
        next_ok = i + expiry
        if np.isnan(yy[i]):
            trades.append((prob.index[i], d, np.nan, 0.0))
            continue
        win = int(yy[i] == d)
        trades.append((prob.index[i], d, win, stake * payout if win else -stake))
    return pd.DataFrame(trades, columns=["time", "dir", "win", "pnl"])


def report(trades: pd.DataFrame, payout: float) -> dict:
    t = trades.dropna(subset=["win"])
    n, w = len(t), int(t["win"].sum()) if len(t) else 0
    be = 1 / (1 + payout)
    wr = w / n if n else 0.0
    eq = t["pnl"].cumsum() if n else pd.Series([0.0])
    dd = float((eq.cummax() - eq).max()) if n else 0.0
    pval = float(stats.binomtest(w, n, be, alternative="greater").pvalue) if n else 1.0
    return {
        "trades": n, "wins": w, "win_rate": wr, "breakeven": be,
        "wilson_low_95": wilson_lower(w, n), "p_value_vs_breakeven": pval,
        "pnl_units": float(t["pnl"].sum()) if n else 0.0,
        "ev_per_trade": float(t["pnl"].mean()) if n else 0.0,
        "max_drawdown": dd,
        "edge_proven": bool(n >= 100 and wilson_lower(w, n) > be),
    }


def pick_threshold(prob, y, grid, expiry, payout, min_trades):
    """Choose threshold on the SELECTION half only; maximise lower-bound-adjusted EV."""
    best, best_score = None, -1e9
    for thr in grid:
        r = report(simulate(prob, y, thr, expiry, payout), payout)
        if r["trades"] < min_trades:
            continue
        score = r["wilson_low_95"] - r["breakeven"]
        if score > best_score:
            best, best_score = thr, score
    return best if best is not None else max(grid)


def fmt(name: str, r: dict) -> str:
    return (f"{name:<14} trades={r['trades']:>5}  win%={r['win_rate']*100:5.1f}  "
            f"breakeven={r['breakeven']*100:4.1f}%  95%CI-low={r['wilson_low_95']*100:5.1f}%  "
            f"EV/trade={r['ev_per_trade']:+.3f}  maxDD={r['max_drawdown']:.1f}  p={r['p_value_vs_breakeven']:.3f}  "
            f"{'EDGE' if r['edge_proven'] else 'no-proven-edge'}")
