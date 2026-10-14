"""Live paper-trading engine shared by the web UI. Optional auto-click on signal."""
import csv
import os
import threading
import time
from collections import deque

import joblib
import pandas as pd

import data as D
from quotex_clicker import QuotexAutoClicker, get_clicker
from backtest import wilson_lower
from features import build_features
from signals import generate_signal


class Engine:
    def __init__(self):
        self.clicker = get_clicker()
        self.cfg = {
            "source": "ccxt",
            "symbol": "BTC/USDT",
            "timeframe": "1m",
            "poll": 5,
            "model": "models/model.joblib",
            "ledger": "paper_ledger.csv",
            "max_consec_losses": 3,
            "max_trades_day": 20,
        }
        self.thread, self.running = None, False
        self.candles = []
        self.feed_status = "idle"
        self.feed_error = None
        self._replay = None
        self._i = 0
        self.reset()
        # Pre-load initial candles in background
        threading.Thread(target=self._initial_fetch, daemon=True).start()

    def reset(self):
        self.s = {
            "status": "stopped",
            "last_bar": None,
            "last_prob": None,
            "last_signal": "WAIT",
            "open_trade": None,
            "wins": 0,
            "n": 0,
            "pnl": 0.0,
            "consec_losses": 0,
            "trades_today": 0,
            "halted": False,
            "clicks": 0,
            "threshold": 0.55,
            "confluence": {"action": "WAIT", "confidence": 0.0, "reasons": []},
        }
        self.log = deque(maxlen=80)
        self.ledger = deque(maxlen=60)
        self._say("Engine initialized.")

    def _initial_fetch(self):
        try:
            df = self._fetch()
            if not df.empty:
                self._update_candles(df)
                self.feed_status = "connected"
        except Exception as e:
            self.feed_status = "error"
            self.feed_error = str(e)

    # ---------- data ----------
    def _fetch(self):
        c = self.cfg
        if c["source"] == "ccxt":
            return D.fetch_ccxt(c["symbol"], c["timeframe"], 200)
        if c["source"] == "yf":
            return D.fetch_yfinance(c["symbol"], "1m", "1d")
        if self._replay is None:
            self._replay = D.synthetic(6000, seed=7, edge=0.4)
            self._i = 300
        self._i += 1  # replay (synthetic) for testing the UI
        if self._i >= len(self._replay):
            self._i = 300
        return self._replay.iloc[: self._i]

    def _update_candles(self, df: pd.DataFrame):
        recs = []
        for t, row in df.tail(120).iterrows():
            ts = int(t.timestamp()) if hasattr(t, "timestamp") else int(time.time())
            recs.append({
                "time": ts,
                "open": round(float(row["open"]), 5),
                "high": round(float(row["high"]), 5),
                "low": round(float(row["low"]), 5),
                "close": round(float(row["close"]), 5),
            })
        self.candles = recs

    def _say(self, msg):
        self.log.appendleft(f"{time.strftime('%H:%M:%S')}  {msg}")

    # ---------- control ----------
    def start(self, **cfg):
        if self.running:
            return True
        self.cfg.update({k: v for k, v in cfg.items() if v is not None})
        if not os.path.exists(self.cfg["model"]):
            self.s["status"] = "no model - run backtest first"
            self._say("Warning: No model.joblib found. Train/Backtest first.")
            return False
        try:
            self.bundle = joblib.load(self.cfg["model"])
            self.s["threshold"] = self.bundle.get("threshold", 0.55)
        except Exception as e:
            self.s["status"] = f"model load error: {e}"
            self._say(f"Error loading model: {e}")
            return False

        if self.cfg["source"] == "replay":
            self._replay = D.synthetic(6000, seed=7, edge=0.4)
            self._i = 300
        if not os.path.exists(self.cfg["ledger"]):
            with open(self.cfg["ledger"], "w", newline="") as f:
                csv.writer(f).writerow(["time", "dir", "entry", "close", "prob", "result", "pnl"])
        self.running = True
        self.s["status"] = "running"
        self._say(f"Trading bot started on {self.cfg['symbol']} ({self.cfg['source']})")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        self.s["status"] = "stopped"
        self._say("Trading bot paused by user.")

    def resume(self):
        self.s["halted"] = False
        self.s["consec_losses"] = 0
        self._say("Bot resumed by user.")

    # ---------- loop ----------
    def _loop(self):
        mdl = self.bundle.get("model")
        thr = self.bundle.get("threshold", 0.55)
        expiry = self.bundle.get("expiry", 1)
        payout = self.bundle.get("payout", 0.85)
        last_bar, trade = None, None

        while self.running:
            try:
                df = self._fetch()
                if df.empty:
                    raise RuntimeError("Empty candle dataframe returned from data feed")

                self._update_candles(df)
                self.feed_status = "connected"
                self.feed_error = None

                # Compute technical confluence signal
                action, conf, reasons = generate_signal(df)
                self.s["confluence"] = {"action": action, "confidence": conf, "reasons": reasons}

                if self.cfg["source"] != "replay":
                    df = df.iloc[:-1]  # drop forming candle

                if df.empty:
                    time.sleep(float(self.cfg["poll"]))
                    continue

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
                        if X.notna().all(axis=1).iloc[0] and mdl is not None:
                            p = float(mdl.predict_proba(X)[0])
                            self.s["last_prob"] = round(p, 4)
                            if p >= thr or p <= 1 - thr:
                                trade = self._open(p, thr, df, bar)
                            else:
                                self.s["last_signal"] = "WAIT"
            except Exception as e:
                self.feed_status = "error"
                self.feed_error = str(e)
                self._say(f"error: {e}")
            time.sleep(0.15 if self.cfg["source"] == "replay" else float(self.cfg["poll"]))

    def _open(self, p, thr, df, bar):
        direction = "CALL" if p >= thr else "PUT"
        self.s["last_signal"] = direction
        trade = {
            "dir": direction,
            "entry": float(df["close"].iloc[-1]),
            "prob": p,
            "bars": 0,
            "time": str(bar),
        }
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
        row = {
            "time": trade["time"],
            "dir": trade["dir"],
            "entry": trade["entry"],
            "close": close,
            "prob": round(trade["prob"], 4),
            "result": res,
            "pnl": pnl,
        }
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
            s["model_info"] = {
                "mode": b.get("mode"),
                "trained_on": b.get("trained_on"),
                "edge_proven": b.get("edge_proven"),
                "test_win_rate": round(100 * b.get("test_win_rate", 0), 1),
                "test_trades": b.get("test_trades"),
                "payout": b.get("payout"),
            }
        cfg_dict = self.clicker.config.model_dump() if hasattr(self.clicker.config, "model_dump") else self.clicker.config.dict()
        return {
            "state": s,
            "candles": self.candles[-100:],
            "log": list(self.log),
            "ledger": list(self.ledger),
            "cfg": self.cfg,
            "feed_status": self.feed_status,
            "feed_error": self.feed_error,
            "autoclick": cfg_dict,
            "click_error": self.clicker.last_error,
            "model_exists": os.path.exists(self.cfg["model"]),
        }

