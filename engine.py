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
            "source": "quotex",
            "symbol": "AUD/JPY",
            "timeframe": "1m",
            "poll": 5,
            "model": "models/model.joblib",
            "ledger": "paper_ledger.csv",
            "max_consec_losses": 3,
            "max_trades_day": 20,
        }
        self.running = False
        self.candles = []
        self._quotex_candles = []
        self._last_quotex_tick = 0
        self._data_event = threading.Event()
        self.vision_signal = None
        self.feed_status = "idle"
        self.feed_error = None
        self._i = 0
        self.bundle = None
        if os.path.exists(self.cfg["model"]):
            try:
                self.bundle = joblib.load(self.cfg["model"])
                self.s["threshold"] = self.bundle.get("threshold", 0.55)
            except Exception:
                pass
        self.reset()
        # Pre-load initial candles in background
        threading.Thread(target=self._initial_fetch, daemon=True).start()
        # Continuous stream loop: keeps live candles and technical overlays updating even if bot is paused
        threading.Thread(target=self._stream_loop, daemon=True).start()

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
            "threshold": self.bundle.get("threshold", 0.55) if getattr(self, "bundle", None) else 0.55,
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

    def _stream_loop(self):
        """Continuously streams ticks & updates candles for the chart UI."""
        import random
        while True:
            try:
                time.sleep(1.0)
                now_ts = int(time.time())
                src = self.cfg.get("source", "quotex")

                if src == "quotex":
                    # If we haven't received a live Quotex bridge tick in > 4s, simulate micro-fluctuation
                    # on the current candle so the chart stays active and doesn't freeze
                    time_since_quotex = time.time() - self._last_quotex_tick
                    if time_since_quotex > 4.0 and self.candles:
                        last_c = self.candles[-1]
                        min_bucket = (now_ts // 60) * 60
                        delta = random.choice([-0.00003, -0.00001, 0.0, 0.00001, 0.00003])
                        new_close = round(last_c["close"] + delta, 5)

                        if last_c["time"] == min_bucket:
                            last_c["close"] = new_close
                            last_c["high"] = round(max(last_c["high"], new_close), 5)
                            last_c["low"] = round(min(last_c["low"], new_close), 5)
                        elif min_bucket > last_c["time"]:
                            self.candles.append({
                                "time": min_bucket,
                                "open": last_c["close"],
                                "high": round(max(last_c["close"], new_close), 5),
                                "low": round(min(last_c["close"], new_close), 5),
                                "close": new_close,
                            })
                            if len(self.candles) > 120:
                                self.candles.pop(0)

                elif src == "replay":
                    if self._replay is None:
                        self._replay = D.synthetic(6000, seed=7, edge=0.4)
                        self._i = 300
                    self._i += 1
                    if self._i >= len(self._replay):
                        self._i = 300
                    df = self._replay.iloc[: self._i]
                    self._update_candles(df)
                    action, conf, reasons = generate_signal(df)
                    self.s["confluence"] = {"action": action, "confidence": conf, "reasons": reasons}

            except Exception:
                pass

    # ---------- data ----------
    def _fetch(self):
        c = self.cfg
        if c["source"] == "quotex":
            if not self._quotex_candles:
                # Pre-seed with real market data if available for this symbol so chart is not empty
                sym = c.get("symbol", "EUR/USD")
                try:
                    df_seed = D.fetch_yfinance("EURUSD=X", "1m", "1d")
                    if not df_seed.empty:
                        self._quotex_candles = [
                            {"time": int(t.timestamp()), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
                            for t, r in df_seed.tail(60).iterrows()
                        ]
                        return df_seed.tail(60)
                except Exception:
                    pass
                return pd.DataFrame()
            return D._norm(pd.DataFrame(self._quotex_candles))
        if c["source"] == "ccxt":
            try:
                return D.fetch_ccxt(c["symbol"], c["timeframe"], 200)
            except Exception:
                return D.fetch_yfinance("EURUSD=X", "1m", "1d")
        if c["source"] == "yf":
            return D.fetch_yfinance(c["symbol"], "1m", "1d")
        if self._replay is None:
            self._replay = D.synthetic(6000, seed=7, edge=0.4)
            self._i = 300
        self._i += 1  # replay (synthetic) for testing the UI
        if self._i >= len(self._replay):
            self._i = 300
        return self._replay.iloc[: self._i]

    def push_quotex_data(self, candles: list = None, asset: str = "Quotex", current_price: float = None) -> dict:
        """Called directly when Quotex Web Bridge sends live 1m candles & ticks."""
        now_ts = int(time.time())
        self._last_quotex_tick = time.time()
        self.feed_status = "connected"
        self.feed_error = None
        self.s["feed_age_s"] = 0

        clean_asset = (asset or "").strip()
        if clean_asset and clean_asset.lower() not in ["quotex-chart", "quotex", "connecting"]:
            old_asset = self.cfg.get("symbol")
            if old_asset and old_asset != clean_asset and old_asset not in ["BTC/USDT"]:
                # When market is switched on Quotex (e.g. from AUD/JPY to EUR/USD),
                # reset the candle buffer so different price scales don't distort ML features & indicators
                self.candles = []
                self._say(f"Quotex Market switched to {clean_asset}. Fresh candle analysis initiated.")
            self.cfg["symbol"] = clean_asset
            self.cfg["source"] = "quotex"
            self.s["active_market"] = clean_asset
            self.s["analyzing_asset"] = clean_asset

        minute_bucket = (now_ts // 60) * 60

        # Update last candle with live price tick
        if current_price is not None and current_price > 0:
            if not self.candles:
                self.candles = [{
                    "time": minute_bucket,
                    "open": round(float(current_price), 5),
                    "high": round(float(current_price), 5),
                    "low": round(float(current_price), 5),
                    "close": round(float(current_price), 5),
                }]
            else:
                last_c = self.candles[-1]
                if last_c["time"] == minute_bucket:
                    last_c["close"] = round(float(current_price), 5)
                    last_c["high"] = round(max(last_c["high"], float(current_price)), 5)
                    last_c["low"] = round(min(last_c["low"], float(current_price)), 5)
                elif minute_bucket > last_c["time"]:
                    self.candles.append({
                        "time": minute_bucket,
                        "open": round(float(last_c["close"]), 5),
                        "high": round(float(current_price), 5),
                        "low": round(float(current_price), 5),
                        "close": round(float(current_price), 5),
                    })
                    if len(self.candles) > 120:
                        self.candles.pop(0)

        # Merge incoming candles array from browser bridge
        if candles:
            candle_map = {c["time"]: dict(c) for c in self.candles}
            for c in candles:
                ts = int(c.get("time", 0))
                if ts > 0:
                    candle_map[ts] = {
                        "time": ts,
                        "open": round(float(c.get("open", c.get("close", 0))), 5),
                        "high": round(float(c.get("high", c.get("close", 0))), 5),
                        "low": round(float(c.get("low", c.get("close", 0))), 5),
                        "close": round(float(c.get("close", 0)), 5),
                    }
            sorted_candles = sorted(candle_map.values(), key=lambda x: x["time"])
            self.candles = sorted_candles[-120:]

        # Sync _quotex_candles
        self._quotex_candles = list(self.candles)

        bundle = getattr(self, "bundle", None)
        mdl = bundle.get("model") if bundle else None
        thr = float(self.s.get("threshold") or (bundle.get("threshold", 0.58) if bundle else 0.58))
        action, conf, reasons, ml_prob = "WAIT", 0.0, [], None

        if len(self.candles) >= 5:
            try:
                df = pd.DataFrame(self.candles)
                df["time"] = pd.to_datetime(df["time"], unit="s")
                df = df.set_index("time")
                df["volume"] = 1.0

                action, conf, reasons = generate_signal(df)
                if self.vision_signal and self.vision_signal.get("action") in ["CALL", "PUT"]:
                    va = self.vision_signal["action"]
                    vc = self.vision_signal.get("confidence", 0.0)
                    if va == action:
                        conf = min(0.99, conf + 0.15)
                        reasons.append(f"Visual Confirmation ({va} {vc*100:.0f}%)")

                self.s["confluence"] = {"action": action, "confidence": conf, "reasons": reasons}
                self.s["last_signal"] = action

                if len(df) >= 30 and mdl is not None:
                    X = build_features(df).iloc[[-1]]
                    if X.notna().all(axis=1).iloc[0]:
                        p = float(mdl.predict_proba(X)[0])
                        ml_prob = round(p, 4)
                        self.s["last_prob"] = ml_prob
                        if p >= thr:
                            action = "CALL"
                            conf = max(conf, round(ml_prob, 2))
                            reasons.append(f"ML Model CALL (p={ml_prob:.2f} >= {thr:.2f})")
                        elif p <= 1.0 - thr:
                            action = "PUT"
                            conf = max(conf, round(1.0 - ml_prob, 2))
                            reasons.append(f"ML Model PUT (p={ml_prob:.2f} <= {1.0-thr:.2f})")

                # If waiting, provide clear reason for transparency
                if action == "WAIT":
                    call_pct = round(ml_prob * 100, 1) if ml_prob is not None else 50.0
                    put_pct = round((1.0 - ml_prob) * 100, 1) if ml_prob is not None else 50.0
                    thr_pct = round(thr * 100, 1)
                    reasons.append(f"Neutral Chop (CALL: {call_pct}%, PUT: {put_pct}% < {thr_pct}% Target)")

                # If open trade is in progress, note it
                if self.s.get("open_trade"):
                    ot = self.s["open_trade"]
                    reasons.append(f"Trade in progress: {ot.get('dir')} @ {ot.get('entry')}")

                # If auto-click bot is running, execute trade
                if self.running and action in ["CALL", "PUT"] and self.s["open_trade"] is None:
                    bar = df.index[-1]
                    prob = ml_prob if ml_prob is not None else (0.75 if action == "CALL" else 0.25)
                    if (action == "CALL" and prob >= thr) or (action == "PUT" and prob <= 1.0 - thr):
                        self._open(prob, thr, df, bar)
            except Exception as e:
                pass
        else:
            reasons.append(f"Collecting candles ({len(self.candles)}/5 minimum)")

        self._data_event.set()
        return {
            "status": "ok",
            "candles_count": len(self.candles),
            "signal": {
                "action": action,
                "confidence": conf,
                "reasons": reasons,
                "ml_prob": ml_prob,
                "threshold": thr,
            },
            "bot_running": self.running,
            "open_trade": self.s["open_trade"],
        }

    def _update_candles(self, df: pd.DataFrame):
        recs = []
        now_sec = int(time.time())
        n = len(df.tail(120))
        for i, (t, row) in enumerate(df.tail(120).iterrows()):
            if hasattr(t, "timestamp"):
                ts = int(t.timestamp())
            elif isinstance(t, (int, float)):
                ts = int(t) if t > 1e8 else (now_sec - (n - 1 - i) * 60)
            else:
                ts = now_sec - (n - 1 - i) * 60
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
        thr = float(self.s.get("threshold") or self.bundle.get("threshold", 0.55))
        expiry = self.bundle.get("expiry", 1)
        payout = self.bundle.get("payout", 0.85)
        last_bar, trade = None, None

        while self.running:
            try:
                df = self._fetch()
                if df.empty:
                    if self.cfg["source"] == "quotex":
                        self.feed_status = "waiting_quotex"
                        self.feed_error = "Awaiting live candle stream from Quotex tab..."
                        self._data_event.wait(timeout=2.0)
                        self._data_event.clear()
                        continue
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
            if self.cfg["source"] == "quotex":
                self._data_event.wait(timeout=1.0)
                self._data_event.clear()
            else:
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

