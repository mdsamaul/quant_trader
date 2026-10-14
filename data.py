import numpy as np
import pandas as pd


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).lower().strip() for c in df.columns]
    for tcol in ("time", "timestamp", "datetime", "date"):
        if tcol in df.columns:
            t = df[tcol]
            if pd.api.types.is_numeric_dtype(t.dtype):
                t = pd.to_datetime(t, unit="ms" if t.iloc[0] > 1e11 else "s")
            else:
                t = pd.to_datetime(t)
            df.index = t
            break
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated()].sort_index().dropna()

    # Reconstruct OHLC bodies & wicks if feed returns flat Open==High==Low==Close ticks (e.g. Yahoo Forex)
    if len(df) > 1 and (df["high"] == df["low"]).mean() > 0.6:
        c = df["close"]
        o = c.shift(1).fillna(c)
        diff = (c - o).abs()
        spr = float(diff[diff > 0].median()) if (diff > 0).any() else 0.0001
        hi = np.maximum(o, c) + spr * 0.35
        lo = np.minimum(o, c) - spr * 0.35
        df["open"] = o
        df["high"] = hi
        df["low"] = lo

    return df


def load_csv(path: str) -> pd.DataFrame:
    return _norm(pd.read_csv(path))


def synthetic(n: int = 60000, seed: int = 1, edge: float = 0.0) -> pd.DataFrame:
    """Random walk with volatility clustering. edge=0 -> no predictable signal at all.
    edge>0 plants a weak mean-reversion effect (for testing the pipeline only)."""
    rng = np.random.default_rng(seed)
    vol = np.empty(n)
    vol[0] = 1e-4
    for i in range(1, n):
        vol[i] = np.sqrt(1e-9 + 0.1 * (vol[i - 1] * rng.standard_normal()) ** 2 + 0.85 * vol[i - 1] ** 2)
    eps = rng.standard_normal(n)
    r = np.zeros(n)
    for i in range(n):
        drift = 0.0
        if edge and i >= 3:
            drift = -edge * vol[i] * np.tanh(r[i - 3:i].sum() / (2 * vol[i] + 1e-12))
        r[i] = vol[i] * eps[i] + drift
    close = 1.1 * np.exp(np.cumsum(r))
    open_ = np.r_[close[0], close[:-1]]
    hi = np.maximum(open_, close) + np.abs(rng.standard_normal(n)) * vol * close * 0.5
    lo = np.minimum(open_, close) - np.abs(rng.standard_normal(n)) * vol * close * 0.5
    idx = pd.date_range("2024-01-01", periods=n, freq="min")
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": 1.0}, index=idx)


def fetch_ccxt(symbol="BTC/USDT", timeframe="1m", bars=50000, exchange="binance") -> pd.DataFrame:
    import ccxt
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    ms = ex.parse_timeframe(timeframe) * 1000
    since = ex.milliseconds() - bars * ms
    rows = []
    while len(rows) < bars:
        chunk = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not chunk:
            break
        rows += chunk
        since = chunk[-1][0] + ms
        if len(chunk) < 2:
            break
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    return _norm(df)


def fetch_yfinance(ticker="EURUSD=X", interval="1m", period="7d") -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, interval=interval, period=period, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index().rename(columns={"Datetime": "time", "Date": "time"})
    return _norm(df)
