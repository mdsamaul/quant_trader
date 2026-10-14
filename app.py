import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, BackgroundTasks, Body, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import pandas as pd

from engine import Engine
from quotex_clicker import get_clicker, execute_click
from quotex_vision import get_vision_tracker
from signals import generate_signal
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("QuantTraderApp")

engine = Engine()

app = FastAPI(title="QuantTrader Quotex Terminal")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")


class AutoClickConfigIn(BaseModel):
    enabled: Optional[bool] = None
    call_x: Optional[int] = None
    call_y: Optional[int] = None
    put_x: Optional[int] = None
    put_y: Optional[int] = None
    restore_cursor: Optional[bool] = None


class FeedConfigIn(BaseModel):
    source: str = "ccxt"       # "ccxt" | "yf" | "replay" | "quotex"
    symbol: str = "BTC/USDT"   # "BTC/USDT", "EURUSD=X", etc.
    timeframe: str = "1m"
    poll: int = 5


class QuotexFeedIn(BaseModel):
    source: str = "quotex"
    asset: Optional[str] = "QUOTEX-CHART"
    candles: list[dict[str, Any]] = []
    current_price: Optional[float] = None
    timestamp: Optional[int] = None


class TestClickIn(BaseModel):
    direction: str = "CALL"    # "CALL" | "PUT"


class GrabCursorIn(BaseModel):
    direction: str = "CALL"
    delay: int = 3


class BacktestRunIn(BaseModel):
    source: str = "synthetic"  # "synthetic" | "ccxt" | "yf" | "csv"
    symbol: Optional[str] = "BTC/USDT"
    bars: int = 6000
    expiry: int = 1
    edge: float = 0.0


@app.get("/")
def get_index():
    return FileResponse("static/index.html")


@app.get("/api/state")
def get_state():
    snap = engine.snapshot()
    st = snap["state"]
    confluence = st.get("confluence", {"action": "WAIT", "confidence": 0.0, "reasons": []})
    
    # Combined signal calculation
    ml_prob = st.get("last_prob")
    thr = st.get("threshold", 0.55)
    
    action = st.get("last_signal", "WAIT")
    confidence = confluence.get("confidence", 0.0)
    reasons = list(confluence.get("reasons", []))

    if ml_prob is not None:
        if ml_prob >= thr:
            action = "CALL"
            confidence = max(confidence, round(ml_prob, 2))
            reasons.append(f"ML Model CALL (p={ml_prob:.2f} >= {thr:.2f})")
        elif ml_prob <= 1.0 - thr:
            action = "PUT"
            confidence = max(confidence, round(1.0 - ml_prob, 2))
            reasons.append(f"ML Model PUT (p={ml_prob:.2f} <= {1.0-thr:.2f})")

    return {
        "symbol": snap["cfg"].get("symbol", "BTC/USDT"),
        "source": snap["cfg"].get("source", "ccxt"),
        "timeframe": snap["cfg"].get("timeframe", "1m"),
        "is_running": engine.running,
        "bot_status": st.get("status", "stopped"),
        "candles": snap.get("candles", []),
        "signal": {
            "action": action,
            "confidence": confidence,
            "reasons": reasons,
            "ml_prob": ml_prob,
        },
        "session": {
            "trades": st.get("n", 0),
            "wins": st.get("wins", 0),
            "win_rate": st.get("win_rate", 0.0),
            "ci_low": st.get("ci_low", 0.0),
            "pnl": st.get("pnl", 0.0),
            "consec_losses": st.get("consec_losses", 0),
            "trades_today": st.get("trades_today", 0),
            "halted": st.get("halted", False),
            "threshold": st.get("threshold", 0.55),
            "breakeven": st.get("breakeven", 54.1),
            "model_info": st.get("model_info"),
            "open_trade": st.get("open_trade"),
        },
        "autoclick": snap.get("autoclick", {}),
        "click_error": snap.get("click_error", ""),
        "feed_status": snap.get("feed_status", "idle"),
        "feed_error": snap.get("feed_error"),
        "logs": snap.get("log", []),
        "ledger": snap.get("ledger", []),
    }


@app.post("/api/bot/start")
def start_bot(config: Optional[FeedConfigIn] = None):
    cfg_args = config.model_dump() if config else {}
    ok = engine.start(**cfg_args)
    return {"status": "ok" if ok else "failed", "running": engine.running, "state": engine.s["status"]}


@app.post("/api/bot/stop")
def stop_bot():
    engine.stop()
    return {"status": "ok", "running": engine.running, "state": engine.s["status"]}


@app.post("/api/bot/toggle")
def toggle_bot():
    if engine.running:
        engine.stop()
    else:
        engine.start()
    return {"status": "ok", "running": engine.running, "state": engine.s["status"]}


@app.post("/api/bot/resume")
def resume_bot():
    engine.resume()
    return {"status": "ok", "halted": engine.s["halted"]}


@app.post("/api/config/autoclick")
def update_autoclick(conf: AutoClickConfigIn):
    clicker = get_clicker()
    kwargs = {k: v for k, v in conf.model_dump().items() if v is not None}
    clicker.update_config(**kwargs)
    cfg_dict = clicker.config.model_dump() if hasattr(clicker.config, "model_dump") else clicker.config.dict()
    return {"status": "ok", "autoclick": cfg_dict}


@app.post("/api/config/feed")
def update_feed(conf: FeedConfigIn):
    was_running = engine.running
    if was_running:
        engine.stop()
    engine.cfg.update(conf.model_dump())
    if was_running:
        engine.start()
    else:
        # Trigger an initial fetch for the new feed in background
        threading.Thread(target=engine._initial_fetch, daemon=True).start()
    return {"status": "ok", "cfg": engine.cfg}


@app.post("/api/click/test")
def test_click(req: TestClickIn):
    clicker = get_clicker()
    res = clicker.test_click(req.direction)
    return res


@app.post("/api/click/scan")
def scan_screen():
    clicker = get_clicker()
    res = clicker.auto_detect_quotex_buttons(delay=1.0)
    return res


@app.post("/api/click/grab")
def grab_cursor(req: GrabCursorIn):
    clicker = get_clicker()
    clicker.capture_after(req.direction, seconds=req.delay)
    return {"status": "ok", "message": f"Hover mouse over {req.direction} button within {req.delay} seconds."}


@app.post("/api/update_data")
def update_candles(background_tasks: BackgroundTasks, candle_data: list[dict[str, Any]] = Body(...)):
    df = pd.DataFrame(candle_data)
    action, conf, reasons = generate_signal(df, min_confidence=0.75)
    
    # Auto-clicker trigger if bot is running
    if engine.running and action in ['CALL', 'PUT']:
        background_tasks.add_task(execute_click, action)

    return {"status": "ok", "signal": {"action": action, "confidence": conf, "reasons": reasons}}


@app.post("/api/quotex/feed")
def receive_quotex_feed(feed: QuotexFeedIn):
    """Real-time candle receiver from Quotex Web Bridge userscript."""
    res = engine.push_quotex_data(feed.candles, asset=feed.asset, current_price=feed.current_price)
    return res


@app.get("/api/quotex/connector.user.js")
def get_connector_script():
    """Serves the Quotex userscript directly so users can click to install or copy."""
    return FileResponse("quotex_connector.user.js", media_type="text/javascript", filename="quotex_connector.user.js")


@app.post("/api/quotex/vision/start")
def start_vision():
    """Start Quotex screen/chart vision tracker."""
    tracker = get_vision_tracker()
    tracker.callback = lambda result: setattr(engine, "vision_signal", result)
    tracker.start()
    return {"status": "ok", "message": "Quotex screen vision tracker started."}


@app.post("/api/quotex/vision/stop")
def stop_vision():
    """Stop Quotex screen/chart vision tracker."""
    tracker = get_vision_tracker()
    tracker.stop()
    engine.vision_signal = None
    return {"status": "ok", "message": "Quotex screen vision tracker stopped."}


@app.get("/api/quotex/vision/status")
def get_vision_status():
    """Check current vision tracker state."""
    tracker = get_vision_tracker()
    return {
        "running": tracker.running,
        "last_status": tracker.last_status,
        "vision_signal": getattr(engine, "vision_signal", None),
    }


@app.post("/api/backtest/run")
def trigger_backtest(background_tasks: BackgroundTasks, params: BacktestRunIn = Body(...)):
    def run_job():
        import subprocess
        cmd = [sys.executable, "run_backtest.py"]
        if params.source == "synthetic":
            cmd += ["--synthetic", "--edge", str(params.edge), "--bars", str(params.bars)]
        elif params.source == "ccxt":
            cmd += ["--ccxt", params.symbol or "BTC/USDT", "--bars", str(params.bars)]
        elif params.source == "yf":
            cmd += ["--yf", params.symbol or "EURUSD=X"]
        cmd += ["--expiry", str(params.expiry)]
        logger.info(f"Running backtest command: {' '.join(cmd)}")
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            engine._say("Walk-forward backtest completed successfully. Model updated.")
        else:
            engine._say(f"Backtest error: {p.stderr[:200]}")

    background_tasks.add_task(run_job)
    return {"status": "ok", "message": "Backtest started in background"}


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)