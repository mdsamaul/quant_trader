"""Walk-forward backtest + model export.

Examples:
  python run_backtest.py --csv data/eurusd_1m.csv --expiry 1
  python run_backtest.py --ccxt BTC/USDT --timeframe 1m --bars 60000
  python run_backtest.py --yf EURUSD=X
  python run_backtest.py --synthetic            # pure noise: must show NO edge
  python run_backtest.py --synthetic --edge 0.3 # planted edge: pipeline must find it
"""
import argparse
import os
import joblib
import numpy as np
import pandas as pd

import data as D
from config import Config
from features import build_features, make_labels
from model import Ensemble
from backtest import simulate, report, pick_threshold, fmt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv"); ap.add_argument("--ccxt"); ap.add_argument("--yf")
    ap.add_argument("--synthetic", action="store_true"); ap.add_argument("--edge", type=float, default=0.0)
    ap.add_argument("--timeframe", default="1m"); ap.add_argument("--bars", type=int, default=60000)
    ap.add_argument("--expiry", type=int, default=1); ap.add_argument("--payout", type=float, default=0.85)
    ap.add_argument("--train", type=int, default=20000); ap.add_argument("--test", type=int, default=2000)
    ap.add_argument("--out", default="models")
    a = ap.parse_args()

    cfg = Config(expiry_bars=a.expiry, payout=a.payout, train_bars=a.train, test_bars=a.test)

    if a.csv: df = D.load_csv(a.csv)
    elif a.ccxt: df = D.fetch_ccxt(a.ccxt, a.timeframe, a.bars)
    elif a.yf: df = D.fetch_yfinance(a.yf)
    else: df = D.synthetic(a.bars, edge=a.edge)
    print(f"Data: {len(df)} candles  {df.index[0]} -> {df.index[-1]}")

    X, y = build_features(df), make_labels(df, cfg.expiry_bars)
    ok = X.notna().all(axis=1)
    X, y = X[ok], y[ok]
    if len(X) < cfg.train_bars + 2 * cfg.test_bars:
        cfg.train_bars = max(2000, int(len(X) * 0.5)); cfg.test_bars = max(500, int(len(X) * 0.1))
        print(f"(small dataset -> train={cfg.train_bars}, test={cfg.test_bars})")

    # ---- walk-forward out-of-sample probabilities (purged by expiry bars) ----
    oof = pd.Series(np.nan, index=X.index); lr_p = oof.copy(); gb_p = oof.copy()
    s = cfg.train_bars
    while s < len(X):
        e = min(s + cfg.test_bars, len(X))
        tr = slice(max(0, s - cfg.train_bars), s - cfg.expiry_bars)
        Xtr, ytr = X.iloc[tr], y.iloc[tr]
        m = ytr.notna()
        mdl = Ensemble(cfg.seed).fit(Xtr[m], ytr[m].astype(int))
        pl, pg = mdl.predict_parts(X.iloc[s:e])
        lr_p.iloc[s:e], gb_p.iloc[s:e], oof.iloc[s:e] = pl, pg, (pl + pg) / 2
        s = e
    valid = oof.notna()
    oof, lr_p, gb_p = oof[valid], lr_p[valid], gb_p[valid]
    yo = y[valid]
    rule_p = 0.5 + 0.5 * X.loc[valid, "rule_score"]

    half = len(oof) // 2
    print(f"\nOut-of-sample candles: {len(oof)} | selection half: {half} | FINAL TEST half: {len(oof)-half}")
    print(f"Breakeven win rate at payout {cfg.payout:.0%}: {cfg.breakeven*100:.1f}%\n")

    final, sel_score = {}, {}
    for name, p in (("rules", rule_p), ("logistic", lr_p), ("gboost", gb_p), ("ensemble", oof)):
        grid = cfg.threshold_grid if name != "rules" else (0.52, 0.55, 0.58, 0.62, 0.66, 0.70)
        thr = pick_threshold(p.iloc[:half], yo.iloc[:half], grid, cfg.expiry_bars, cfg.payout, cfg.min_trades_for_threshold)
        tr = simulate(p.iloc[half:], yo.iloc[half:], thr, cfg.expiry_bars, cfg.payout)
        r = report(tr, cfg.payout); final[name] = (thr, r)
        rs = report(simulate(p.iloc[:half], yo.iloc[:half], thr, cfg.expiry_bars, cfg.payout), cfg.payout)
        sel_score[name] = rs["wilson_low_95"] - rs["breakeven"]
        print(fmt(f"{name}@{thr:.2f}", r))

    ml = ("logistic", "gboost", "ensemble")
    best = max(ml, key=lambda k: sel_score[k])   # chosen on the selection half only (not on the test half)
    thr_b, rep_b = final[best]
    label = a.ccxt or a.yf or a.csv or "synthetic"
    print(f"\nEXPORTED MODEL: {best} (picked on the selection half) | trained on: {label} | payout {cfg.payout:.0%}")
    print(f"  its untouched-test result: {rep_b['trades']} trades, {rep_b['win_rate']*100:.1f}% win, "
          f"95%CI-low {rep_b['wilson_low_95']*100:.1f}% vs breakeven {rep_b['breakeven']*100:.1f}%")
    print("  VERDICT: " + ("EDGE proven on test data for THIS instrument/period."
          if rep_b["edge_proven"] else "NO PROVEN EDGE. Live signals from this model are not better than chance."))
    print("An 80% win rate is not achievable by any known method on liquid 1-minute binary options; ~55-58% would already be excellent.")
    print("The model only applies to the instrument it was trained on. Re-test on another period before trusting it.")

    # ---- export final model trained on the latest window ----
    os.makedirs(a.out, exist_ok=True)
    Xl, yl = X.iloc[-cfg.train_bars:], y.iloc[-cfg.train_bars:]
    m = yl.notna()
    mdl = Ensemble(cfg.seed, mode=best).fit(Xl[m], yl[m].astype(int))
    joblib.dump({"model": mdl, "threshold": thr_b, "expiry": cfg.expiry_bars, "payout": cfg.payout,
                 "mode": best, "trained_on": label, "edge_proven": bool(rep_b["edge_proven"]),
                 "test_win_rate": rep_b["win_rate"], "test_trades": rep_b["trades"],
                 "test_ci_low": rep_b["wilson_low_95"]}, os.path.join(a.out, "model.joblib"))
    print(f"Saved {a.out}/model.joblib")

if __name__ == "__main__":
    main()
