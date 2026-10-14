import numpy as np
import pandas as pd
from indicators import ema, sma, rsi, atr, macd, bollinger, stochastic, adx, streak
from signals import candle_parts, rule_score


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    a = atr(df, 14).replace(0, np.nan)
    f = pd.DataFrame(index=df.index)

    ret = c.pct_change()
    vol20 = ret.rolling(20).std().replace(0, np.nan)
    for k in (1, 2, 3, 5, 10):
        f[f"ret_{k}"] = c.pct_change(k) / vol20
    for n in (9, 21, 50):
        f[f"ema_dist_{n}"] = (c - ema(c, n)) / a
    f["ema_slope_21"] = ema(c, 21).diff(3) / a
    f["ema_cross_9_21"] = (ema(c, 9) - ema(c, 21)) / a
    f["rsi_14"] = rsi(c, 14)
    f["rsi_7"] = rsi(c, 7)
    _, _, mh = macd(c)
    f["macd_hist"] = mh / a
    f["macd_hist_d"] = mh.diff() / a
    pb, bw = bollinger(c)
    f["bb_pctb"] = pb
    f["bb_width"] = bw
    k, d = stochastic(df)
    f["stoch_k"], f["stoch_kd"] = k, k - d
    f["adx"] = adx(df)
    f["atr_pct"] = a / c
    f["vol_ratio"] = ret.rolling(5).std() / vol20
    f["z_20"] = (c - sma(c, 20)) / c.rolling(20).std().replace(0, np.nan)
    body, up_w, lo_w = candle_parts(df)
    f["body"], f["upper_wick"], f["lower_wick"] = body, up_w, lo_w
    f["candle_dir"] = np.sign(c - df["open"])
    f["streak"] = streak(c).clip(-6, 6)
    f["rule_score"] = rule_score(df)
    if isinstance(df.index, pd.DatetimeIndex):
        h = df.index.hour + df.index.minute / 60
        f["hour_sin"], f["hour_cos"] = np.sin(2 * np.pi * h / 24), np.cos(2 * np.pi * h / 24)
    return f.replace([np.inf, -np.inf], np.nan)


def make_labels(df: pd.DataFrame, expiry: int) -> pd.Series:
    """1 = close[t+expiry] > close[t] (CALL wins), 0 = lower (PUT wins), NaN = tie / unknown."""
    fut = df["close"].shift(-expiry)
    y = pd.Series(np.nan, index=df.index)
    y[fut > df["close"]] = 1.0
    y[fut < df["close"]] = 0.0
    return y
