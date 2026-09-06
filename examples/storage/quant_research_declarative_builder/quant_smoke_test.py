"""Runs INSIDE the sandbox (written there by build_quant_research_image.py via
POST /v1/sandboxes/{id}/files, then executed via POST /v1/sandboxes/{id}/exec).

Not meant to be run on your own machine unless you've separately pip
installed the same five pinned packages -- see this directory's README.

Proves all five quant-research packages the image was built with
(vectorbt, backtrader, TA-Lib, QuantLib, quantstats) actually import and
run together, using a synthetic price series generated in-process --
no network egress needed, matching the sandbox's default-deny network
posture (see docs/DECLARATIVE-BUILDER-DESIGN.md and the "self-hosted quant
research agent for banks" blog post this example follows on from).
"""

import numpy as np
import pandas as pd
import talib
import vectorbt as vbt
import backtrader as bt
import QuantLib as ql
import quantstats as qs

print("vectorbt", vbt.__version__)
print("backtrader", bt.__version__)
print("QuantLib", ql.__version__)
print("quantstats", qs.__version__)

# Synthetic daily price series -- stands in for a real market-data feed a
# researcher would normally load from an internal source inside the
# firm's own network.
rng = np.random.default_rng(7)
n = 500
returns = rng.normal(loc=0.0004, scale=0.012, size=n)
dates = pd.date_range("2024-01-01", periods=n, freq="D")
prices = pd.Series(100 * np.cumprod(1 + returns), index=dates, name="close")

# TA-Lib: fast/slow SMA crossover signal.
sma_fast = talib.SMA(prices.values, timeperiod=10)
sma_slow = talib.SMA(prices.values, timeperiod=30)
valid = ~np.isnan(sma_fast) & ~np.isnan(sma_slow)
entries = (sma_fast > sma_slow) & valid
exits = (sma_fast < sma_slow) & valid

# vectorbt: vectorized backtest of that same crossover.
pf = vbt.Portfolio.from_signals(prices, entries, exits, init_cash=100_000, fees=0.001)
print("vectorbt total return: %.4f" % pf.total_return())

# quantstats: risk/return stats on the strategy's own return stream.
strategy_returns = pf.returns()
print("quantstats Sharpe: %.4f" % qs.stats.sharpe(strategy_returns))
print("quantstats max drawdown: %.4f" % qs.stats.max_drawdown(strategy_returns))

# QuantLib: an independent pricing sanity check (fixed-rate bond) -- not
# tied to the synthetic series above, just proving the C++ bindings work.
today = ql.Date(15, 1, 2026)
ql.Settings.instance().evaluationDate = today
schedule = ql.Schedule(
    today,
    today + ql.Period(2, ql.Years),
    ql.Period(ql.Semiannual),
    ql.TARGET(),
    ql.Following,
    ql.Following,
    ql.DateGeneration.Backward,
    False,
)
bond = ql.FixedRateBond(2, 100.0, schedule, [0.05], ql.Actual360())
curve = ql.FlatForward(today, 0.03, ql.Actual360())
bond.setPricingEngine(ql.DiscountingBondEngine(ql.YieldTermStructureHandle(curve)))
print("QuantLib bond NPV: %.4f" % bond.NPV())


# backtrader: the same crossover strategy again, via its event-driven
# engine instead of vectorbt's vectorized one -- the two libraries solve
# the same backtesting problem with genuinely different execution models
# (see this directory's README), so both get exercised here.
class SmaCross(bt.Strategy):
    params = dict(fast=10, slow=30)

    def __init__(self):
        sma_fast = bt.ind.SMA(period=self.p.fast)
        sma_slow = bt.ind.SMA(period=self.p.slow)
        self.crossover = bt.ind.CrossOver(sma_fast, sma_slow)

    def next(self):
        if not self.position and self.crossover > 0:
            self.buy()
        elif self.position and self.crossover < 0:
            self.close()


ohlcv = pd.DataFrame(
    {
        "open": prices,
        "high": prices * 1.001,
        "low": prices * 0.999,
        "close": prices,
        "volume": 0,
    },
    index=dates,
)

cerebro = bt.Cerebro()
cerebro.broker.setcash(100_000.0)
cerebro.adddata(bt.feeds.PandasData(dataname=ohlcv))
cerebro.addstrategy(SmaCross)
cerebro.run()
print("backtrader ending cash: %.2f" % cerebro.broker.getvalue())

print("OK: all five quant-research packages installed via the declarative builder, imported, and ran.")
