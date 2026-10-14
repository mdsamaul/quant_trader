"""Live PAPER trading on a real price feed. No clicking, no real orders.
  python live_paper.py --ccxt BTC/USDT --timeframe 1m
  python live_paper.py --yf EURUSD=X
Logs every trade to paper_ledger.csv and prints rolling stats with a confidence interval.
"""
import argparse
import csv
import os
import time
import joblib
import pandas as pd

import data as D
from features import build_features
from backtest import wilson_lower


def fetch(a):
    if a.ccxt: return D.fetch_ccxt(a.ccxt, a.timeframe, 400)
    return D.fetch_yfinance(a.yf, "1m", "1d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccxt"); ap.add_argument("--yf"); ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--model", default="models/model.joblib"); ap.add_argument("--ledger", default="paper_ledger.csv")
    a = ap.parse_args()
    bundle = joblib.load(a.model)
    mdl, thr, expiry, payout = bundle["model"], bundle["threshold"], bundle["expiry"], bundle["payout"]
    open_trade, wins, n, pnl = None, 0, 0, 0.0
    if not os.path.exists(a.ledger):
        with open(a.ledger, "w", newline="") as f:
            csv.writer(f).writerow(["time", "dir", "entry", "close", "prob", "result", "pnl"])
    last_bar = None
    while True:
        df = fetch(a).iloc[:-1]            # drop the still-forming candle
        bar = df.index[-1]
        if bar != last_bar:
            last_bar = bar
            if open_trade is not None:
                open_trade["bars"] += 1
                if open_trade["bars"] >= expiry:
                    close = df["close"].iloc[-1]
                    win = (close > open_trade["entry"]) if open_trade["dir"] == "CALL" else (close < open_trade["entry"])
                    tie = close == open_trade["entry"]
                    res = "TIE" if tie else ("WIN" if win else "LOSS")
                    p = 0.0 if tie else (payout if win else -1.0)
                    n += 0 if tie else 1; wins += int(win and not tie); pnl += p
                    with open(a.ledger, "a", newline="") as f:
                        csv.writer(f).writerow([bar, open_trade["dir"], open_trade["entry"], close, round(open_trade["prob"], 4), res, p])
                    lo = wilson_lower(wins, n) * 100
                    print(f"[{bar}] {res}  trades={n} win%={100*wins/max(n,1):.1f} (95%CI low {lo:.1f}%) pnl={pnl:+.2f}")
                    open_trade = None
            if open_trade is None:
                X = build_features(df).iloc[[-1]]
                if X.notna().all(axis=1).iloc[0]:
                    p = float(mdl.predict_proba(X)[0])
                    if p >= thr or p <= 1 - thr:
                        open_trade = {"dir": "CALL" if p >= thr else "PUT", "entry": df["close"].iloc[-1], "prob": p, "bars": 0}
                        print(f"[{bar}] SIGNAL {open_trade['dir']} p={p:.3f} entry={open_trade['entry']}")
        time.sleep(5)


if __name__ == "__main__":
    main()
