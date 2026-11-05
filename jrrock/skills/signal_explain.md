---
name: signal_explain
description: Explains a trading signal in plain language with entry/exit reasoning
triggers: [signal, explain, why, entry, strategy]
---

# Signal Explanation Skill

When asked to explain a signal or strategy, produce a clear, structured breakdown.

## Format

```
SIGNAL EXPLANATION
  Symbol: BTCUSDT
  Direction: LONG
  Strategy: golden_cross_enhanced + vegas_tunnel_bullish

  WHY THIS SIGNAL:
  - MA50 just crossed above MA200 (golden cross) — historically a major trend reversal.
  - Volume on the cross candle was 1.4x the 20-period average — confirms institutional buying.
  - Price is above the Vegas tunnel (EMA144 > EMA169) — medium-term trend is up.
  - Three strategies agree across 3 timeframes (15m, 1h, 4h) — strong confluence.

  ENTRY: Market order at current price (~$X)

  TP: $X (+2.5% from entry) — R/R 2.5:1
  SL: $X (-1.0% from entry) — ~50% of way to liquidation

  RISK:
  - If price drops below MA50 ($X) within 2 candles, the signal is likely false.
  - Watch for volume drop on next candle — confirms or invalidates momentum.
  - Funding rate is +0.02% — slightly expensive to hold long. Monitor.

  CONFIDENCE: 82%
```

Always be honest about risks. Never omit the SL or the risk section.
