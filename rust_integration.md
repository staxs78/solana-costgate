# Integrating the cost gate into a Rust searcher

This is the drop-in shape for the gate, matching the model in `costgate.py`.
It is deliberately a pure function: the searcher computes a candidate route and a
gross profit, then calls the gate before it builds/signs a transaction.

```rust
const BPS: u64 = 10_000;
const LAMPORTS_PER_SOL: u64 = 1_000_000_000;

#[derive(Clone, Debug)]
pub struct CostGate {
    pub base_fee_lamports: u64,        // 5_000 * signatures
    pub cu_limit: u64,
    pub cu_price_microlamports: u64,   // µLamports per CU
    pub jito_tip_lamports: u64,
    pub tip_on_failure: bool,
    pub land_probability: f64,         // 0.0..=1.0
    pub flashloan_fee_bps: u64,
    pub flashloan_principal_lamports: u64,
    pub ata_rent_lamports: u64,
    pub slippage_buffer_bps: u64,
    pub sol_price_usd: f64,            // for USD reporting only
    pub min_profit_usd: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Verdict {
    pub go: bool,
    pub expected_net_lamports: f64,    // can be negative
    pub gross_spread_bps: f64,
    pub break_even_spread_bps: f64,
}

impl CostGate {
    /// priority fee is charged on the *requested* CU limit, even on failure
    pub fn priority_fee_lamports(&self) -> u64 {
        (self.cu_price_microlamports as f64 * self.cu_limit as f64 / 1_000_000.0).ceil() as u64
    }

    pub fn flash_fee_lamports(&self) -> u64 {
        (self.flashloan_fee_bps as f64 / BPS as f64
            * self.flashloan_principal_lamports as f64)
            .ceil() as u64
    }

    /// `gross_profit_lamports` = (out - in) of the whole atomic cycle, in lamports.
    /// `notional_lamports` = the input leg value in lamports.
    pub fn evaluate(&self, gross_profit_lamports: f64, notional_lamports: f64) -> Verdict {
        let slip = self.slippage_buffer_bps as f64 / BPS as f64;
        let gross_after_slip = gross_profit_lamports * (1.0 - slip);
        let p = self.land_probability;

        let fixed = (self.base_fee_lamports + self.priority_fee_lamports()) as f64;
        let success = (self.jito_tip_lamports + self.flash_fee_lamports()
            + self.ata_rent_lamports) as f64;
        let mut cost = fixed + p * success;
        if self.tip_on_failure {
            cost += (1.0 - p) * self.jito_tip_lamports as f64;
        }

        let expected_net_lamports = p * gross_after_slip - cost;
        let expected_net_usd =
            expected_net_lamports / LAMPORTS_PER_SOL as f64 * self.sol_price_usd;

        // break-even spread (bps) for this notional
        let s = notional_lamports / LAMPORTS_PER_SOL as f64; // notional in SOL
        let denom = p * s * (1.0 - slip);
        let break_even_spread_bps = if denom > 0.0 {
            BPS as f64 * ((fixed + p * success) / LAMPORTS_PER_SOL as f64) / denom
        } else {
            f64::INFINITY
        };

        Verdict {
            go: expected_net_usd > self.min_profit_usd,
            expected_net_lamports,
            gross_spread_bps: gross_profit_lamports / notional_lamports * BPS as f64,
            break_even_spread_bps,
        }
    }
}
```

## Where it plugs in

```
loop {
    // 1. ingest pool state (Yellowstone gRPC / ZeroMQ / RPC) -> updated reserves
    // 2. detect candidate routes, compute their gross output with the DEX simulators
    //    (constant-product math == the Pool::swap_out model in costgate.py)
    let gross = route.output(amount_in) - amount_in;          // lamports
    let v = gate.evaluate(gross as f64, notional_lamports as f64);

    if !v.go {
        // log and DROP: this is where a gross-diff-only bot silently bleeds
        metrics.rejected(v.break_even_spread_bps);
        continue;
    }

    // 3. estimate CU with simulateTransaction (don't guess), refresh priority fee,
    //    size the tip from the live tip stream, then build v0/v1 tx and send.
}
```

## Two things not to get wrong

- **`notional_lamports` must be the input leg value in SOL terms**, not the raw
  token amount, or the break-even spread is meaningless.
- **Do not double-count price impact.** The AMM output already contains it;
  `slippage_buffer_bps` is an *extra* safety margin, so keep it small (0–5 bps)
  unless you intentionally want to be conservative.

The Python version in this directory is the test oracle: `tests/test_costgate.py`
pins the expected behaviour (break-even consistency, monotonicity under tip /
priority-fee / land-probability / slippage changes, min profitable size, and the
non-AMM hop). Port those assertions to Rust so both implementations cannot drift.
