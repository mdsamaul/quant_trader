import numpy as np
import pandas as pd
from indicators import ema, rsi, atr, bollinger, adx


def candle_parts(df: pd.DataFrame):
    """
    Returns normalized (body, upper_wick, lower_wick) relative to ATR.
    """
    a = atr(df, 14).replace(0, np.nan)
    body = (df["close"] - df["open"]) / a
    up_w = (df["high"] - df[["open", "close"]].max(axis=1)) / a
    lo_w = (df[["open", "close"]].min(axis=1) - df["low"]) / a
    return body.fillna(0), up_w.fillna(0), lo_w.fillna(0)


def rule_score(df: pd.DataFrame) -> pd.Series:
    """
    Regime-aware vote for binary options (-1.0 = strong PUT, +1.0 = strong CALL).
    ADX switches between trend-following (EMA) and mean-reversion (RSI, Bollinger %B)
    with candlestick pattern bonuses (hammer, shooting star, engulfing).
    """
    c = df["close"]
    o = df["open"]
    h = df["high"]
    l = df["low"]

    a = atr(df, 14).replace(0, np.nan)
    adx_val = adx(df, 14).fillna(20)

    # 1. Trend component
    e21 = ema(c, 21)
    e50 = ema(c, 50)
    trend_raw = (e21 - e50) / a
    trend_vote = np.tanh(trend_raw.fillna(0))

    # 2. Mean reversion component (RSI + Bollinger %B)
    rsi_val = rsi(c, 14).fillna(50)
    rsi_vote = (50 - rsi_val) / 25.0  # oversold (<30) -> positive; overbought (>70) -> negative
    rsi_vote = rsi_vote.clip(-1.0, 1.0)

    pct_b, _ = bollinger(c, 20, 2.0)
    pct_b = pct_b.fillna(0.5)
    bb_vote = ((0.5 - pct_b) * 2.0).clip(-1.0, 1.0)

    mr_vote = 0.5 * rsi_vote + 0.5 * bb_vote

    # 3. Candlestick patterns
    body_abs = (c - o).abs()
    up_wick = h - df[["open", "close"]].max(axis=1)
    lo_wick = df[["open", "close"]].min(axis=1) - l

    pattern_vote = pd.Series(0.0, index=df.index)

    # Hammer (bullish rejection)
    hammer = (lo_wick >= 2 * body_abs) & (up_wick <= 0.5 * body_abs) & (body_abs > 0.05 * a)
    pattern_vote[hammer] += 0.5

    # Shooting Star / Inverted Hammer (bearish rejection)
    star = (up_wick >= 2 * body_abs) & (lo_wick <= 0.5 * body_abs) & (body_abs > 0.05 * a)
    pattern_vote[star] -= 0.5

    # Bullish / Bearish Engulfing
    prev_c = c.shift(1)
    prev_o = o.shift(1)
    bull_engulf = (prev_c < prev_o) & (c > o) & (o <= prev_c) & (c >= prev_o)
    bear_engulf = (prev_c > prev_o) & (c < o) & (o >= prev_c) & (c <= prev_o)
    pattern_vote[bull_engulf] += 0.6
    pattern_vote[bear_engulf] -= 0.6

    pattern_vote = pattern_vote.clip(-1.0, 1.0)

    # 4. Regime weighting based on ADX (trend strength)
    # ADX >= 30: strong trend -> trend weight high
    # ADX <= 20: ranging/flat -> mean-reversion weight high
    trend_weight = ((adx_val - 15) / 20.0).clip(0.1, 0.9)
    mr_weight = 1.0 - trend_weight

    score = (trend_weight * trend_vote) + (mr_weight * mr_vote) + (0.3 * pattern_vote)
    return score.clip(-1.0, 1.0).fillna(0.0)


def calculate_indicators(df):
    df = df.copy()
    # EMA Trend
    df['ema_9'] = ema(df['close'], 9)
    df['ema_21'] = ema(df['close'], 21)
    df['ema_50'] = ema(df['close'], 50)
    
    # RSI (14)
    df['rsi'] = rsi(df['close'], 14)

    # Bollinger Bands
    df['bb_mid'] = df['close'].rolling(window=20).mean()
    df['bb_std'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_mid'] + (df['bb_std'] * 2)
    df['bb_lower'] = df['bb_mid'] - (df['bb_std'] * 2)

    # ATR
    df['atr'] = atr(df, 14)
    return df


def generate_signal(df, min_confidence=0.75):
    """
    Ensemble rule for Quotex binary options (1m-5m expiry).
    Returns: action ('CALL', 'PUT', 'WAIT'), confidence (float), reasons (list)
    """
    if len(df) < 50:
        return 'WAIT', 0.0, ['Data Insufficient']
    
    df = calculate_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    call_score = 0.0
    put_score = 0.0
    total_checks = 5.0
    reasons = []

    # 1. Trend Direction (EMA 21 vs 50)
    if last['ema_21'] > last['ema_50']:
        call_score += 1.0
        reasons.append("Uptrend (EMA 21 > 50)")
    elif last['ema_21'] < last['ema_50']:
        put_score += 1.0
        reasons.append("Downtrend (EMA 21 < 50)")

    # 2. RSI Exhaustion & Rebound
    if last['rsi'] < 32 and last['rsi'] > prev['rsi']:
        call_score += 1.2
        reasons.append("RSI Oversold Bounce")
    elif last['rsi'] > 68 and last['rsi'] < prev['rsi']:
        put_score += 1.2
        reasons.append("RSI Overbought Pullback")

    # 3. Bollinger Band Rejection
    if last['low'] <= last['bb_lower'] and last['close'] > last['open']:
        call_score += 1.2
        reasons.append("Lower BB Rejection Candle")
    elif last['high'] >= last['bb_upper'] and last['close'] < last['open']:
        put_score += 1.2
        reasons.append("Upper BB Rejection Candle")

    # 4. Candlestick Momentum / EMA 9 crossover
    if prev['close'] < prev['ema_9'] and last['close'] > last['ema_9']:
        call_score += 1.0
        reasons.append("Bullish EMA 9 Cross")
    elif prev['close'] > prev['ema_9'] and last['close'] < last['ema_9']:
        put_score += 1.0
        reasons.append("Bearish EMA 9 Cross")

    # 5. Volatility Check (Ignore flat markets)
    atr_mean = df['atr'].mean()
    if np.isnan(last['atr']) or (atr_mean > 0 and last['atr'] < atr_mean * 0.6):
        return 'WAIT', 0.0, ['Market Flat / Low Volatility']

    # Final Decision Calculation
    call_conf = min(round(call_score / total_checks, 2), 0.99)
    put_conf = min(round(put_score / total_checks, 2), 0.99)

    if call_conf >= min_confidence and call_conf > put_conf:
        return 'CALL', call_conf, reasons
    elif put_conf >= min_confidence and put_conf > call_conf:
        return 'PUT', put_conf, reasons
    else:
        wait_reasons = list(reasons)
        wait_reasons.append(f"Technical Confluence: {int(max(call_conf, put_conf)*100)}% (Target: {int(min_confidence*100)}%)")
        return 'WAIT', max(call_conf, put_conf), wait_reasons