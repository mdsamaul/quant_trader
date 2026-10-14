from dataclasses import dataclass


@dataclass
class Config:
    expiry_bars: int = 1          # trade expiry in candles (1 = next candle close)
    payout: float = 0.85          # broker payout on win (Quotex ~0.80-0.90)
    train_bars: int = 20000       # rolling training window
    test_bars: int = 2000         # walk-forward test block
    min_trades_for_threshold: int = 40
    threshold_grid: tuple = (0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70)
    seed: int = 42

    @property
    def breakeven(self) -> float:
        return 1.0 / (1.0 + self.payout)
