import numpy as np
import pandas as pd

def calculate_indicators(df):
    # EMA Trend
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    # RSI (14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-9)
    df['rsi'] = 100 - (100 / (1 + rs))

    # Bollinger Bands
    df['bb_mid'] = df['close'].rolling(window=20).mean()
    df['bb_std'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_mid'] + (df['bb_std'] * 2)
    df['bb_lower'] = df['bb_mid'] - (df['bb_std'] * 2)

    # ATR for Volatility filter
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift()), abs(df['low'] - df['close'].shift()))
    )
    df['atr'] = df['tr'].rolling(window=14).mean()
    return df

def generate_signal(df, min_confidence=0.75):
    """
    Ensemble rule for Quotex binary options (1m-5m expiry).
    Returns: action ('CALL', 'PUT', 'WAIT'), confidence (float), reasons (list)
    """
    if len(df) < 50:
        return 'WAIT', 0.0, ['Data Insufficient']
    
    df = calculate_indicators(df.copy())
    last = df.iloc[-1]
    prev = df.iloc[-2]

    call_score = 0
    put_score = 0
    total_checks = 5
    reasons = []

    # 1. Trend Direction (EMA 21 vs 50)
    if last['ema_21'] > last['ema_50']:
        call_score += 1
        reasons.append("Uptrend (EMA 21 > 50)")
    elif last['ema_21'] < last['ema_50']:
        put_score += 1
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
    if last['atr'] < df['atr'].mean() * 0.6:
        return 'WAIT', 0.0, ['Market Flat / Low Volatility']

    # Final Decision Calculation
    call_conf = min(round(call_score / total_checks, 2), 0.99)
    put_conf = min(round(put_score / total_checks, 2), 0.99)

    if call_conf >= min_confidence and call_conf > put_conf:
        return 'CALL', call_conf, reasons
    elif put_conf >= min_confidence and put_conf > call_conf:
        return 'PUT', put_conf, reasons
    else:
        return 'WAIT', max(call_conf, put_conf), ['Confidence below threshold']