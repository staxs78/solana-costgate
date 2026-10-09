"""costgate - after-cost profitability gate for atomic Solana arbitrage cycles.

Stdlib-only Python 3.10+. See README.md for the model and the exact formulas.

The gate answers one question: given a detected cross-DEX price difference,
does the *expected value after all costs* clear a threshold - including the
costs that are charged even when a transaction fails (base fee + priority fee),
the success-only tip / flashloan fee, and the probability that another searcher
wins the race?

Nothing in here talks to a network: it is a pure model so it can be unit-tested
and reused as a reference implementation next to a Rust searcher.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LAMPORTS_PER_SOL = 1_000_000_000
BPS = 10_000


# --------------------------------------------------------------------------- #
# Route model
# --------------------------------------------------------------------------- #
@dataclass
class Pool:
    """A single swap hop.

    AMM hop: constant-product (x*y=k) with a fee charged on the input amount.
    Non-AMM hop: set ``expected_out_ratio`` (net out/in, after fees) and it is
    used verbatim instead of the AMM math (e.g. a CLOB fill or aggregator quote).
    """

    name: str
    reserve_in: float = 0.0
    reserve_out: float = 0.0
    fee_bps: float = 0.0
    expected_out_ratio: Optional[float] = None

    def swap_out(self, amount_in: float) -> float:
        if amount_in <= 0:
            return 0.0
        if self.expected_out_ratio is not None:
            return amount_in * self.expected_out_ratio
        amount_after_fee = amount_in * (1.0 - self.fee_bps / BPS)
        if self.reserve_in <= 0:
            raise ValueError(f"pool {self.name!r}: reserve_in must be > 0")
        return (self.reserve_out * amount_after_fee) / (self.reserve_in + amount_after_fee)


@dataclass
class Route:
    """An ordered chain of hops that starts and ends in the same token."""

    name: str
    hops: List[Pool] = field(default_factory=list)

    def output(self, amount_in: float) -> float:
        amt = amount_in
        for hop in self.hops:
            amt = hop.swap_out(amt)
        return amt

    def gross_profit(self, amount_in: float) -> float:
        return self.output(amount_in) - amount_in


# --------------------------------------------------------------------------- #
# Cost / execution assumptions
# --------------------------------------------------------------------------- #
@dataclass
class Assumptions:
    """Every knob that turns a gross spread into an expected net profit."""

    sol_price_usd: float = 180.0
    token_usd_price: float = 1.0          # USD price of 1 whole start token
    signatures: int = 1
    base_fee_lamports: int = 5_000        # per signature (Solana default)

    compute_unit_limit: int = 300_000
    compute_unit_price_microlamports: int = 10_000  # uLamports per CU

    jito_tip_lamports: int = 1_000        # paid only on success by default
    tip_on_failure: bool = False

    land_probability: float = 1.0         # P(this submission is the winner)
    flashloan_fee_bps: float = 0.0
    flashloan_principal_sol: float = 0.0
    ata_rent_lamports: int = 0

    slippage_buffer_bps: float = 0.0      # extra buffer on top of modelled impact
    min_profit_usd: float = 0.0           # threshold to return GO


# --------------------------------------------------------------------------- #
# Core math
# --------------------------------------------------------------------------- #
def priority_fee_lamports(a: Assumptions) -> int:
    """ceil(cu_price * cu_limit / 1e6). Charged on failure too, and based on the
    *requested* CU limit, so an inflated limit is paid for even if unused."""
    return math.ceil(a.compute_unit_price_microlamports * a.compute_unit_limit / 1_000_000)


def _usd_to_lamports(value_usd: float, a: Assumptions) -> float:
    if a.sol_price_usd <= 0:
        raise ValueError("sol_price_usd must be > 0")
    return value_usd / a.sol_price_usd * LAMPORTS_PER_SOL


def _flash_fee_lamports(a: Assumptions) -> int:
    return math.ceil(a.flashloan_fee_bps / BPS * a.flashloan_principal_sol * LAMPORTS_PER_SOL)


def evaluate(route: Route, start_amount: float, a: Assumptions) -> Dict[str, Any]:
    """Return the full after-cost picture for one cycle at one trade size."""
    if start_amount <= 0:
        raise ValueError("start_amount must be > 0")
    if not 0.0 <= a.land_probability <= 1.0:
        raise ValueError("land_probability must be within [0, 1]")

    gross_out = route.output(start_amount)
    slip = a.slippage_buffer_bps / BPS
    gross_out_after_slip = gross_out * (1.0 - slip)
    gross_profit = gross_out_after_slip - start_amount
    gross_profit_usd = gross_profit * a.token_usd_price
    spread_bps = gross_profit / start_amount * BPS

    base = a.base_fee_lamports * a.signatures
    priority = priority_fee_lamports(a)
    tip = a.jito_tip_lamports
    flash = _flash_fee_lamports(a)
    ata = a.ata_rent_lamports

    fixed_per_attempt = base + priority              # charged win or lose
    success_only = tip + flash + ata                 # only if the tx lands

    p = a.land_probability
    ev_cost_lamports = fixed_per_attempt + p * success_only
    if a.tip_on_failure:
        ev_cost_lamports += (1.0 - p) * tip

    ev_gross_lamports = p * _usd_to_lamports(gross_profit_usd, a)
    ev_net_lamports = ev_gross_lamports - ev_cost_lamports
    ev_net_usd = ev_net_lamports / LAMPORTS_PER_SOL * a.sol_price_usd

    start_value_usd = start_amount * a.token_usd_price
    roi = ev_net_usd / start_value_usd if start_value_usd > 0 else 0.0

    return {
        "route": route.name,
        "start_amount": start_amount,
        "start_value_usd": start_value_usd,
        "gross_out": gross_out,
        "gross_profit": gross_profit,
        "gross_profit_usd": gross_profit_usd,
        "gross_spread_bps": spread_bps,
        "land_probability": p,
        "costs_lamports": {
            "base_fee": base,
            "priority_fee": priority,
            "jito_tip": tip,
            "flashloan_fee": flash,
            "ata_rent": ata,
            "fixed_per_attempt": fixed_per_attempt,
            "success_only": success_only,
            "expected_total": ev_cost_lamports,
        },
        "expected_gross_lamports": ev_gross_lamports,
        "expected_net_lamports": ev_net_lamports,
        "expected_net_usd": ev_net_usd,
        "expected_net_sol": ev_net_lamports / LAMPORTS_PER_SOL,
        "break_even_spread_bps": break_even_spread_bps(route, start_amount, a),
        "roi": roi,
        "decision": "GO" if ev_net_usd > a.min_profit_usd else "NO-GO",
        "reason": (
            "expected net clears threshold"
            if ev_net_usd > a.min_profit_usd
            else "expected net does not clear threshold (costs/land probability eat the spread)"
        ),
    }


def break_even_spread_bps(route: Route, start_amount: float, a: Assumptions) -> float:
    """Gross-spread (bps of notional, before slippage buffer) at which EV == 0.

    Solving  p * (spread/1e4) * S * (1 - s) = (B+P)/1e9 + p*(T+F+A)/1e9  for spread,
    where S = notional in SOL, s = slippage-buffer fraction.
    """
    if a.sol_price_usd <= 0:
        raise ValueError("sol_price_usd must be > 0")
    start_value_sol = (start_amount * a.token_usd_price) / a.sol_price_usd
    if start_value_sol <= 0:
        raise ValueError("notional must be > 0")
    p = a.land_probability
    if p <= 0:
        return float("inf")
    fixed = (a.base_fee_lamports * a.signatures + priority_fee_lamports(a)) / LAMPORTS_PER_SOL
    success = (a.jito_tip_lamports + _flash_fee_lamports(a) + a.ata_rent_lamports) / LAMPORTS_PER_SOL
    slip_frac = a.slippage_buffer_bps / BPS
    denom = p * start_value_sol * (1.0 - slip_frac)
    if denom <= 0:
        return float("inf")
    return BPS * (fixed + p * success) / denom


def min_profitable_size(
    route: Route,
    a: Assumptions,
    cap: Optional[float] = None,
    min_profit_usd: Optional[float] = None,
) -> Optional[float]:
    """Smallest notional (in whole start tokens) whose expected net profit clears
    the threshold, or None if no size up to ``cap`` does.

    EV(size) is unimodal: fixed costs dominate at tiny sizes and price impact
    erodes the edge at huge sizes, so we scan a geometric grid for the first
    crossing then bisect.
    """
    threshold = a.min_profit_usd if min_profit_usd is None else min_profit_usd
    if cap is None:
        reserves = [h.reserve_in for h in route.hops if h.reserve_in > 0]
        cap = (max(reserves) * 10) if reserves else 1e9

    def ev(size: float) -> float:
        try:
            return evaluate(route, size, a)["expected_net_usd"] - threshold
        except ValueError:
            return float("-inf")

    n = 400
    lo_log, hi_log = math.log10(1e-9), math.log10(max(cap, 1e-9))
    prev_x: Optional[float] = None
    crossing: Optional[tuple] = None
    for i in range(n + 1):
        x = 10 ** (lo_log + (hi_log - lo_log) * i / n)
        y = ev(x)
        if prev_x is not None and y > 0 and ev(prev_x) <= 0:
            crossing = (prev_x, x)
            break
        prev_x = x
    if crossing is None:
        return None

    lo, hi = crossing
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        if ev(mid) > 0:
            hi = mid
        else:
            lo = mid
    return hi


# --------------------------------------------------------------------------- #
# Config loading / CLI
# --------------------------------------------------------------------------- #
def route_from_dict(d: Dict[str, Any]) -> Route:
    hops = [
        Pool(
            name=h.get("name", f"hop{idx}"),
            reserve_in=float(h.get("reserve_in", 0.0)),
            reserve_out=float(h.get("reserve_out", 0.0)),
            fee_bps=float(h.get("fee_bps", 0.0)),
            expected_out_ratio=(
                None if h.get("expected_out_ratio") is None else float(h["expected_out_ratio"])
            ),
        )
        for idx, h in enumerate(d.get("hops", []))
    ]
    return Route(name=d.get("name", "route"), hops=hops)


def assumptions_from_dict(d: Optional[Dict[str, Any]]) -> Assumptions:
    known = set(Assumptions.__dataclass_fields__)  # type: ignore[attr-defined]
    clean = {k: v for k, v in (d or {}).items() if k in known}
    return Assumptions(**clean)


def run_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    a = assumptions_from_dict(cfg.get("assumptions"))
    route = route_from_dict(cfg.get("route", {}))
    start_amount = float(cfg.get("start_amount", 1.0))
    result = evaluate(route, start_amount, a)
    result["min_profitable_size"] = min_profitable_size(route, a)
    return result


def _fmt(res: Dict[str, Any]) -> str:
    c = res["costs_lamports"]
    mps = res.get("min_profitable_size")
    lines = [
        f"Route: {res['route']}  |  notional {res['start_amount']:,.6g}"
        f" ({res['start_value_usd']:,.2f} USD)",
        f"  gross out         : {res['gross_out']:,.6f}",
        f"  gross profit      : {res['gross_profit']:,.6f}"
        f"  ({res['gross_spread_bps']:.1f} bps, {res['gross_profit_usd']:,.4f} USD)",
        f"  land probability  : {res['land_probability']:.3f}",
        "  costs (lamports)  :",
        f"    base fee        : {c['base_fee']:,}",
        f"    priority fee    : {c['priority_fee']:,}",
        f"    jito tip        : {c['jito_tip']:,}",
        f"    flashloan fee   : {c['flashloan_fee']:,}",
        f"    ata rent        : {c['ata_rent']:,}",
        f"    fixed/attempt   : {c['fixed_per_attempt']:,}",
        f"    expected total  : {c['expected_total']:,.0f}",
        f"  expected gross    : {res['expected_net_lamports'] + c['expected_total']:,.0f} lamports",
        f"  expected net      : {res['expected_net_lamports']:,.0f} lamports"
        f"  ({res['expected_net_usd']:,.4f} USD, {res['expected_net_sol']:.9f} SOL)",
        f"  break-even spread : {res['break_even_spread_bps']:.2f} bps",
        f"  min profitable sz : {'none' if mps is None else f'{mps:,.6g}'}",
        f"  ROI (per attempt) : {res['roi']*100:.4f}%",
        f"  DECISION          : {res['decision']}  ({res['reason']})",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="After-cost arbitrage profitability gate")
    ap.add_argument("--config", required=True, help="path to a JSON config")
    ap.add_argument("--json", action="store_true", help="emit raw JSON instead of a report")
    args = ap.parse_args(argv)

    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    result = run_config(cfg)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_fmt(result))
    return 0 if result["decision"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
