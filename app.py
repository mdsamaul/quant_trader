from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
import pandas as pd
from signals import generate_signal
from quotex_clicker import execute_click

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

state = {
    "symbol": "EURUSD_OTC",
    "candles": [],
    "last_signal": {"action": "WAIT", "confidence": 0.0, "reasons": []},
    "is_running": False
}

@app.get("/")
def get_index():
    return FileResponse("static/index.html")

@app.get("/api/state")
def get_state():
    return {
        "symbol": state["symbol"],
        "candles": state["candles"][-100:] if state["candles"] else [],
        "signal": state["last_signal"]
    }

@app.post("/api/update_data")
def update_candles(candle_data: list, background_tasks: BackgroundTasks):
    df = pd.DataFrame(candle_data)
    action, conf, reasons = generate_signal(df, min_confidence=0.75)
    state["last_signal"] = {"action": action, "confidence": conf, "reasons": reasons}
    state["candles"] = candle_data
    
    # Auto-clicker trigger
    if state["is_running"] and action in ['CALL', 'PUT']:
        background_tasks.add_task(execute_click, action)
        
    return {"status": "ok", "signal": state["last_signal"]}

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)