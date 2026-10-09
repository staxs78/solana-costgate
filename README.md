# costgate — after-cost profitability gate for atomic Solana arbitrage

[![tests](https://github.com/staxs78/solana-costgate/actions/workflows/tests.yml/badge.svg)](https://github.com/staxs78/solana-costgate/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![deps](https://img.shields.io/badge/dependencies-none-lightgrey)

A small, dependency-free reference implementation of the missing piece in most
public MEV/arbitrage repos: a gate that decides whether a *detected* cross-DEX
price difference is worth submitting **after every cost that is actually
charged** — including the fees you pay even when the transaction loses the race.

The gross spread a searcher sees is not the profit. The profit is:

```
expected_net = land_probability × gross_profit_after_slippage
             − (base_fee + priority_fee)                      # paid every attempt
             − land_probability × (jito_tip + flashloan_fee + rent)
```

If that number does not clear a threshold, the honest answer is **NO-GO**.

## Why this exists

A 2026 Solana searcher is dominated by two facts that a gross-`diff` check hides:

1. **The base fee and the priority fee are charged on failed transactions too.**
   Every lost race is real spend (priority fee is `ceil(cu_price × cu_limit / 1e6)`
   and is charged on the *requested* CU limit, not the CU actually used).
2. **You win a race with probability `p < 1`.** A route that looks +50 bps gross
   can be expected-value negative once you amortize the losing submissions and
   the tip/jito/flashloan costs you only pay when you land.

A complete gate also has to model AMM price impact (constant-product `x·y=k`) so
that "bigger trade" stops looking free.

## Install / run

No dependencies — Python 3.10+, stdlib only.

```bash
python costgate.py --config examples/two_hop.json          # human report (exit 0 = GO, 1 = NO-GO)
python costgate.py --config examples/three_hop.json --json # machine-readable
python -m unittest discover -s tests -v                    # 14 tests
```

See `example_report.md` for sample output and `rust_integration.md` for where
this plugs into a Rust searcher loop.

## Model

### Route
An ordered list of hops that starts and ends in the same token.

- **AMM hop** (default): constant-product, fee charged on the input.
  ```
  in_after_fee = amount_in × (1 − fee_bps/10000)
  out          = reserve_out × in_after_fee / (reserve_in + in_after_fee)
  ```
- **Non-AMM hop**: provide `expected_out_ratio` (net out/in, after fees) and the
  AMM math is skipped — use this for a CLOB fill or an aggregator/quote you
  already have.

### Costs (itemised in every result)
| Cost | Formula | Paid on failure? |
|---|---|---|
| Base fee | `base_fee_lamports × signatures` (default 5,000/sig) | **yes** |
| Priority fee | `ceil(cu_price_microlamports × cu_limit / 1e6)` | **yes** |
| Jito tip | config (`jito_tip_lamports`) | no (default; `tip_on_failure` to override) |
| Flashloan fee | `flashloan_fee_bps/1e4 × principal_sol × 1e9` | no |
| ATA / rent | `ata_rent_lamports` | no |

Expected cost per submission:

```
expected_cost = (base + priority) + p × (tip + flashloan + rent) [+ (1−p) × tip if tip_on_failure]
```

### Decision
```
ev_gross = p × gross_profit_after_slippage
ev_net   = ev_gross − expected_cost
GO  iff ev_net > min_profit_usd
```

### Break-even spread
The gross spread (bps of notional, before the slippage buffer) at which `ev_net = 0`:

```
break_even_bps = 1e4 × [ (B+P)/1e9 + p·(T+F+A)/1e9 ] / ( p · S · (1 − s) )
```
where `S` = notional in SOL, `s` = slippage-buffer fraction, `B/P/T/F/A` =
base / priority / tip / flashloan / rent lamports.

### Minimum profitable size
`min_profitable_size()` scans a geometric grid for the smallest notional whose
`ev_net` clears the threshold, then bisects. `EV(size)` is unimodal: fixed costs
dominate at tiny sizes and price impact erodes the edge at large sizes.

## Config schema
```jsonc
{
  "start_amount": 1000.0,                 // whole start tokens
  "assumptions": { "sol_price_usd": 180.0, "token_usd_price": 1.0 },
  "route": {
    "name": "USDC->SOL->USDC",
    "hops": [
      { "name": "raydium", "reserve_in": 1000000, "reserve_out": 5000, "fee_bps": 25 },
      { "name": "clob",    "expected_out_ratio": 1.05 }
    ]
  }
}
```
All `Assumptions` fields are optional and defaulted.

## Assumptions & limitations
- Prices are supplied by the caller (`sol_price_usd`, `token_usd_price`); the tool
  does no price fetching. This is deliberate: it is a pure function so it can be
  unit-tested and embedded.
- The AMM model is the constant-product form matching the standard Raydium/Orca
  pool math. Concentrated-liquidity (CLMM/Whirlpool) venues need their own tick
  math — feed those as `expected_out_ratio` until you drop in a CLMM simulator.
- `land_probability` and `gross_profit` are *estimates*; the gate is only as good
  as the numbers you feed it. The value is that it makes both explicit and
  rejectable, instead of hiding them in a gross `diff`.
- Slippage is applied once (a buffer on the final output). Price impact is already
  inside the AMM math, so avoid double counting: keep `slippage_buffer_bps` small
  unless you mean a margin of safety.

## License
MIT — see `LICENSE`.

---

## Need this wired into a real searcher?

This repo is a standalone reference. If you want it applied to *your* router, I
offer a fixed-scope, no-private-alpha review:

- **After-cost gate review (free first step).** Send the numbers for one current
  route (pool reserves, amounts, fee tiers, your CU price/limit and tip policy)
  and I'll return this gate's verdict plus a short written explanation — at no
  charge, so you can judge the analysis before paying.
- **Router cost-control roadmap (paid, fixed fee).** A written audit of your
  detection → simulation → submission loop against the 2026 Solana cost model:
  where failed-tx spend leaks, how to size against break-even, priority-fee and
  Jito-tip policy, and a step-by-step integration of this gate. Delivered as a
  document plus a runnable integration patch against the public interfaces you
  name.

Engagement and pricing are agreed before any work starts. Payment in
USDC/USDT on Base (or Solana). This is operated as a disclosed AI-assisted
studio — no impersonation, no fabricated credentials, and no private alpha is
requested or disclosed.

Open an issue on this repo titled `gate review` with a public-safe description,
or reach the maintainer via the issue tracker.
