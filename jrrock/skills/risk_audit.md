---
name: risk_audit
description: Audit and risk management skill — finds and mitigates exposure
triggers: [risk, exposure, danger, liquidation, margin]
---

# Risk Audit Skill

When this skill is active, your primary lens is **risk management**.

## Checklist

1. **Liquidation distance**: For every open position, compute the distance (%) from
   current price to estimated liquidation. Flag any position where distance < 5%.

2. **SL coverage**: Every position must have an active SL order. Report unprotected positions
   immediately as CRITICAL.

3. **Total exposure**: Sum all notional values. Flag if total notional > 5x account equity.

4. **Concentration**: If more than 60% of open notional is in one symbol, flag it.

5. **Daily PnL**: If daily PnL < -3%, recommend halting autotrade.

## Output format
```
RISK AUDIT — <timestamp>
  Positions: N open
  Total notional: $X
  Worst liq distance: X%  [flag if < 5%]
  Unprotected positions: [CRITICAL if any]
  Daily PnL: X%  [HALT if < -3%]
  Recommendation: [HOLD | REDUCE | HALT]
```
