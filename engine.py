"""Live paper-trading engine shared by the web UI. Optional auto-click on signal."""
import csv
import os
import threading
import time
from collections import deque

import joblib
import pandas as pd

import data as D
from quotex_clicker import QuotexAutoClicker
from backtest import wilson_lower
from features import build_features


class Engine:
    def __init__(self):
        self.clicker = QuotexAutoClicker()
        self.cfg = {"source": "ccxt", "symbol": "BTC/USDT", "timeframe": "1m", "poll": 5,
                    "model": "models/model.joblib", "ledger": "paper_ledger.csv",
                    "max_consec_losses": 3, "max_trades_day": 20}
        self.thread, self.running = None, False
        self.reset()

    def reset(self):
        self.s = {"status": "stopped", "last_bar": None, "last_prob": None, "last_signal": None,
                  "open_trade": None, "wins": 0, "n": 0, "pnl": 0.0, "consec_losses": 0,
                  "trades_today": 0, "halted": False, "clicks": 0, "threshold": None}
        self.log = deque(maxlen=80)
        self.ledger = deque(maxlen=60)

    # ---------- data ----------
    def _fetch(self):
        c = self.cfg
        if c["source"] == "ccxt":
            return D.fetch_ccxt(c["symbol"], c["timeframe"], 400)
        if c["source"] == "yf":
            return D.fetch_yfinance(c["symbol"], "1m", "1d")
        self._i += 1  # replay (synthetic) for testing the UI
        return self._replay.iloc[:self._i]

    def _say(self, msg):
        self.log.appendleft(f"{time.strftime('%H:%M:%S')}  {msg}")

    # ---------- control ----------
    def start(self, **cfg):
        if self.running:
            return False
        self.cfg.update({k: v for k, v in cfg.items() if v is not None})
        if not os.path.exists(self.cfg["model"]):
            self.s["status"] = "no model - train first"
            return False
        self.bundle = joblib.load(self.cfg["model"])
        self.reset()
        self.s["threshold"] = self.bundle["threshold"]
        if self.cfg["source"] == "replay":
            self._replay = D.synthetic(6000, seed=7, edge=0.4)
            self._i = 300
        if not os.path.exists(self.cfg["ledger"]):
            with open(self.cfg["ledger"], "w", newline="") as f:
                csv.writer(f).writerow(["time", "dir", "entry", "close", "prob", "result", "pnl"])
        self.running = True
        self.s["status"] = "running"
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        self.s["status"] = "stopped"

    def resume(self):
        self.s["halted"] = False
        self.s["consec_losses"] = 0
        self._say("resumed by user")

    # ---------- loop ----------
    def _loop(self):
        mdl, thr = self.bundle["model"], self.bundle["threshold"]
        expiry, payout = self.bundle["expiry"], self.bundle["payout"]
        last_bar, trade = None, None
        while self.running:
            try:
                df = self._fetch()
                if self.cfg["source"] != "replay":
                    df = df.iloc[:-1]  # drop forming candle
                bar = df.index[-1]
                if self.cfg["source"] != "replay":
                    ts = pd.Timestamp(bar)
                    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                    age = (pd.Timestamp.now(tz="UTC") - ts).total_seconds()
                    self.s["feed_age_s"] = int(age)
                    if age > 240 and bar != last_bar:
                        self._say(f"warning: data feed is {int(age)}s old - signals may be late")
                if bar != last_bar:
                    last_bar = bar
                    self.s["last_bar"] = str(bar)
                    if trade is not None:
                        trade["bars"] += 1
                        if trade["bars"] >= expiry:
                            trade = self._settle(trade, df, payout)
                    if trade is None:
                        X = build_features(df).iloc[[-1]]
                        if X.notna().all(axis=1).iloc[0]:
                            p = float(mdl.predict_proba(X)[0])
                            self.s["last_prob"] = round(p, 4)
                            if p >= thr or p <= 1 - thr:
                                trade = self._open(p, thr, df, bar)
                            else:
                                self.s["last_signal"] = "SKIP"
            except Exception as e:  # keep running on network hiccups
                self._say(f"error: {e}")
            time.sleep(0.15 if self.cfg["source"] == "replay" else float(self.cfg["poll"]))

    def _open(self, p, thr, df, bar):
        direction = "CALL" if p >= thr else "PUT"
        self.s["last_signal"] = direction
        trade = {"dir": direction, "entry": float(df["close"].iloc[-1]), "prob": p, "bars": 0, "time": str(bar)}
        self.s["open_trade"] = trade
        self.s["trades_today"] += 1
        clicked = False
        if self.s["halted"]:
            self._say(f"{direction} signal ignored (halted)")
        elif self.s["trades_today"] > int(self.cfg["max_trades_day"]):
            self._say("daily trade limit reached - no click")
        else:
            clicked = self.clicker.execute_click(direction)
            self.s["clicks"] += int(clicked)
        self._say(f"SIGNAL {direction} p={p:.3f} entry={trade['entry']:.5f}" + (" [CLICKED]" if clicked else ""))
        return trade

    def _settle(self, trade, df, payout):
        close = float(df["close"].iloc[-1])
        tie = close == trade["entry"]
        win = (close > trade["entry"]) if trade["dir"] == "CALL" else (close < trade["entry"])
        res = "TIE" if tie else ("WIN" if win else "LOSS")
        pnl = 0.0 if tie else (payout if win else -1.0)
        if not tie:
            self.s["n"] += 1
            self.s["wins"] += int(win)
            self.s["pnl"] += pnl
            self.s["consec_losses"] = 0 if win else self.s["consec_losses"] + 1
        row = {"time": trade["time"], "dir": trade["dir"], "entry": trade["entry"], "close": close,
               "prob": round(trade["prob"], 4), "result": res, "pnl": pnl}
        self.ledger.appendleft(row)
        with open(self.cfg["ledger"], "a", newline="") as f:
            csv.writer(f).writerow(list(row.values()))
        self.s["open_trade"] = None
        self._say(f"{res} {trade['dir']} pnl={pnl:+.2f}")
        if self.s["consec_losses"] >= int(self.cfg["max_consec_losses"]) and not self.s["halted"]:
            self.s["halted"] = True
            self._say(f"HALTED: {self.s['consec_losses']} losses in a row. Auto-click paused until you press Resume.")
        return None

    def snapshot(self):
        s = dict(self.s)
        n, w = s["n"], s["wins"]
        s["win_rate"] = round(100 * w / n, 1) if n else 0.0
        s["ci_low"] = round(100 * wilson_lower(w, n), 1) if n else 0.0
        s["breakeven"] = round(100 / (1 + self.bundle["payout"]), 1) if hasattr(self, "bundle") else 54.1
        s["pnl"] = round(s["pnl"], 2)
        if hasattr(self, "bundle"):
            b = self.bundle
            s["model_info"] = {"mode": b.get("mode"), "trained_on": b.get("trained_on"), "edge_proven": b.get("edge_proven"),
                               "test_win_rate": round(100 * b.get("test_win_rate", 0), 1), "test_trades": b.get("test_trades"),
                               "payout": b.get("payout")}
        return {"state": s, "log": list(self.log), "ledger": list(self.ledger), "cfg": self.cfg,
                "autoclick": self.clicker.config.dict(), "click_error": self.clicker.last_error, "model_exists": os.path.exists(self.cfg["model"])}
