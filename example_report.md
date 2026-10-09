# costgate - demo report

Generated from the bundled examples. gross = raw detected spread; net = expected value after base fee, priority fee, tip, flashloan fee, rent, slippage buffer and land probability.

## Example 1 - two-hop AMM arb (Raydium AMMv4 vs Orca Whirlpool)

```
Route: USDC->SOL->USDC (Raydium AMMv4 vs Orca Whirlpool)  |  notional 1,000 (1,000.00 USD)
  gross out         : 1,102.689919
  gross profit      : 102.469381  (1024.7 bps, 102.4694 USD)
  land probability  : 0.350
  costs (lamports)  :
    base fee        : 5,000
    priority fee    : 3,000
    jito tip        : 50,000,000
    flashloan fee   : 0
    ata rent        : 0
    fixed/attempt   : 8,000
    expected total  : 17,508,000
  expected gross    : 199,246,018 lamports
  expected net      : 181,738,018 lamports  (32.7128 USD, 0.181738018 SOL)
  break-even spread : 90.06 bps
  min profitable sz : 113.479
  ROI (per attempt) : 3.2713%
  DECISION          : GO  (expected net clears threshold)
```

## Example 2 - three-hop mixed-venue arb (AMM + CLOB fill + stable swap, flashloan-funded)

```
Route: USDC->SOL->USDT->USDC (AMM + CLOB fill + stable swap)  |  notional 1,000 (1,000.00 USD)
  gross out         : 1,021.622919
  gross profit      : 21.112107  (211.1 bps, 21.1121 USD)
  land probability  : 0.500
  costs (lamports)  :
    base fee        : 5,000
    priority fee    : 11,250
    jito tip        : 20,000,000
    flashloan fee   : 2,775,000
    ata rent        : 2,039,280
    fixed/attempt   : 16,250
    expected total  : 12,423,390
  expected gross    : 58,644,742 lamports
  expected net      : 46,221,352 lamports  (8.3198 USD, 0.046221352 SOL)
  break-even spread : 44.75 bps
  min profitable sz : 389.835
  ROI (per attempt) : 0.8320%
  DECISION          : GO  (expected net clears threshold)
```

