# quant_trader — real-data walk-forward binary-options research + paper trader

## Run
```bash
pip install -r requirements.txt
python run_backtest.py --ccxt BTC/USDT --timeframe 1m --bars 60000     # real crypto data
python run_backtest.py --yf EURUSD=X                                  # real forex (yfinance, ~7 days 1m)
python run_backtest.py --csv data/your_1m.csv --expiry 1              # your own OHLC csv (time,open,high,low,close)
python run_backtest.py --synthetic                                    # noise sanity check: must say NO edge
python run_backtest.py --synthetic --edge 0.4                         # planted edge: must say EDGE
python live_paper.py --ccxt BTC/USDT --timeframe 1m                   # live paper trading, logs paper_ledger.csv
```

## UI (dashboard + auto-click)
```bash
python app.py        # then open http://localhost:8000
```
1. Train & Backtest (বা `replay` সোর্স দিয়ে ডেমো) 2. Start 3. Auto-click চালু করতে CALL/PUT position set করো, Test দিয়ে যাচাই করো, তারপর checkbox on।
Safety: Auto-click default off; N টানা loss হলে auto halt; daily trade limit। শুধু Demo account।

## ⚡ Quotex Real-Time Integration (Live Chart Sync & Vision)
1. **Web Bridge (0ms Latency - Recommended):**
   - কোটেক্সের লাইভ OTC ও রিয়েল পেয়ারের ১-মিনিটের ক্যান্ডেল সরাসরি ব্রাউজার থেকে পাইথন ইঞ্জিনে স্ট্রিম করতে [quotex_connector.user.js](quotex_connector.user.js) ব্যবহার করুন (Tampermonkey বা Chrome Console-এ চালান)।
   - ব্রাউজারে Quotex চার্টের উপরেই লাইভ AI প্রবাবিলিটি HUD দেখাবে এবং স্বয়ংক্রিয়ভাবে ইঞ্জিনে সিগন্যাল পাঠাবে।
2. **Screen Vision (OpenCV):**
   - OpenCV ও Win32 API দিয়ে Quotex উইন্ডো ট্র্যাক করে ক্যান্ডেলের রঙ ও মোমেন্টাম বিশ্লেষণ করা যায়। UI-এর "Quotex Connect" থেকে এক ক্লিকে চালু করা যায়।

## Algorithms
- Indicators: EMA 9/21/50, RSI 7/14, MACD, Bollinger %B/width, Stochastic, ADX, ATR, candle body/wick, streaks, time-of-day
- `rules`: regime-aware vote (ADX switches trend-following vs mean-reversion weights) + hammer/engulfing patterns
- `logistic` + `gboost` (shallow, regularised) -> `ensemble` (average probability)
- Walk-forward retraining, purge gap = expiry, threshold chosen on selection half, reported on untouched test half
- One trade at a time (no overlapping expiries), ties refunded

## How to judge results
- Breakeven win rate = 1/(1+payout) (85% payout -> 54.1%).
- Trust a result only if `EDGE` appears (>=100 trades and 95% Wilson lower bound > breakeven) on the FINAL TEST half.
- Never judge by 4-of-5 trades; that is pure luck (a coin flip hits 4/5+ about 19% of the time).
- Quotex OTC/broker feeds are not public; data from other feeds may not match Quotex prices.
