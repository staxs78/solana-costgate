"""Unit tests for the costgate after-cost profitability gate.

Run from the deliverable directory:
    python -m unittest discover -s tests -v
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import costgate as cg  # noqa: E402


def profitable_route() -> cg.Route:
    return cg.Route(
        name="USDC->SOL->USDC",
        hops=[
            cg.Pool("raydium", reserve_in=1_000_000.0, reserve_out=5_000.0, fee_bps=25),
            cg.Pool("orca", reserve_in=4_500.0, reserve_out=1_000_000.0, fee_bps=30),
        ],
    )


def base_assumptions(**over):
    a = cg.Assumptions(
        sol_price_usd=180.0,
        token_usd_price=1.0,
        compute_unit_limit=300_000,
        compute_unit_price_microlamports=10_000,
        jito_tip_lamports=50_000_000,
        land_probability=0.35,
        slippage_buffer_bps=2,
        min_profit_usd=1.0,
    )
    for k, v in over.items():
        setattr(a, k, v)
    return a


class TestBasicGate(unittest.TestCase):
    def test_priority_fee_formula(self):
        a = cg.Assumptions(compute_unit_limit=300_000, compute_unit_price_microlamports=10_000)
        # ceil(300000 * 10000 / 1_000_000) = 3000
        self.assertEqual(cg.priority_fee_lamports(a), 3_000)

    def test_two_hop_profitable_is_go(self):
        res = cg.evaluate(profitable_route(), 1000.0, base_assumptions())
        self.assertEqual(res["decision"], "GO")
        self.assertGreater(res["expected_net_usd"], 0)
        self.assertGreater(res["gross_spread_bps"], 0)

    def test_equal_price_pools_are_no_go(self):
        r = cg.Route(
            name="flat",
            hops=[
                cg.Pool("a", reserve_in=1_000_000.0, reserve_out=5_000.0, fee_bps=25),
                cg.Pool("b", reserve_in=5_000.0, reserve_out=1_000_000.0, fee_bps=30),
            ],
        )
        res = cg.evaluate(r, 1000.0, base_assumptions())
        self.assertEqual(res["decision"], "NO-GO")
        self.assertLess(res["expected_net_usd"], 0)

    def test_huge_tip_turns_route_into_no_go(self):
        res = cg.evaluate(profitable_route(), 1000.0, base_assumptions(jito_tip_lamports=10_000_000_000))
        self.assertEqual(res["decision"], "NO-GO")


class TestCostSensitivity(unittest.TestCase):
    def test_land_probability_is_monotonic(self):
        lo = cg.evaluate(profitable_route(), 1000.0, base_assumptions(land_probability=0.2))
        hi = cg.evaluate(profitable_route(), 1000.0, base_assumptions(land_probability=1.0))
        self.assertLess(lo["expected_net_usd"], hi["expected_net_usd"])

    def test_higher_priority_fee_reduces_net(self):
        low = cg.evaluate(profitable_route(), 1000.0, base_assumptions(compute_unit_price_microlamports=1_000))
        high = cg.evaluate(profitable_route(), 1000.0, base_assumptions(compute_unit_price_microlamports=1_000_000))
        self.assertLess(high["expected_net_usd"], low["expected_net_usd"])

    def test_flashloan_fee_reduces_net(self):
        noflash = cg.evaluate(profitable_route(), 1000.0, base_assumptions())
        flash = cg.evaluate(
            profitable_route(), 1000.0,
            base_assumptions(flashloan_fee_bps=10, flashloan_principal_sol=5.5),
        )
        self.assertLess(flash["expected_net_usd"], noflash["expected_net_usd"])
        self.assertGreater(flash["costs_lamports"]["flashloan_fee"], 0)

    def test_tip_on_failure_increases_expected_cost(self):
        off = cg.evaluate(profitable_route(), 1000.0, base_assumptions(tip_on_failure=False))
        on = cg.evaluate(profitable_route(), 1000.0, base_assumptions(tip_on_failure=True))
        self.assertLess(on["expected_net_usd"], off["expected_net_usd"])

    def test_slippage_buffer_reduces_net(self):
        off = cg.evaluate(profitable_route(), 1000.0, base_assumptions(slippage_buffer_bps=0))
        on = cg.evaluate(profitable_route(), 1000.0, base_assumptions(slippage_buffer_bps=50))
        self.assertLess(on["expected_net_usd"], off["expected_net_usd"])


class TestBreakEven(unittest.TestCase):
    def test_break_even_spread_gives_zero_net(self):
        a = base_assumptions(slippage_buffer_bps=0)
        flat = cg.Route("flat", hops=[cg.Pool("x", expected_out_ratio=1.0)])
        be = cg.break_even_spread_bps(flat, 1000.0, a)
        self.assertGreater(be, 0)
        # build a route whose gross spread (in bps) equals the break-even spread
        r = cg.Route("at_be", hops=[cg.Pool("x", expected_out_ratio=1.0 + be / cg.BPS)])
        res = cg.evaluate(r, 1000.0, a)
        self.assertAlmostEqual(res["expected_net_usd"], 0.0, places=6)

    def test_break_even_scales_with_land_probability(self):
        a_low = base_assumptions(land_probability=0.1)
        a_high = base_assumptions(land_probability=1.0)
        flat = cg.Route("flat", hops=[cg.Pool("x", expected_out_ratio=1.0)])
        self.assertGreater(
            cg.break_even_spread_bps(flat, 1000.0, a_low),
            cg.break_even_spread_bps(flat, 1000.0, a_high),
        )


class TestMinProfitableSize(unittest.TestCase):
    def test_min_size_found_and_boundary(self):
        a = base_assumptions()
        mps = cg.min_profitable_size(profitable_route(), a)
        self.assertIsNotNone(mps)
        self.assertEqual(cg.evaluate(profitable_route(), mps, a)["decision"], "GO")
        self.assertEqual(cg.evaluate(profitable_route(), mps * 0.5, a)["decision"], "NO-GO")

    def test_flat_route_has_no_profitable_size(self):
        r = cg.Route(
            "flat",
            hops=[
                cg.Pool("a", reserve_in=1_000_000.0, reserve_out=5_000.0, fee_bps=25),
                cg.Pool("b", reserve_in=5_000.0, reserve_out=1_000_000.0, fee_bps=30),
            ],
        )
        self.assertIsNone(cg.min_profitable_size(r, base_assumptions()))


class TestConfigRoundTrip(unittest.TestCase):
    def test_run_config_uses_expected_out_ratio(self):
        cfg = {
            "start_amount": 100.0,
            "assumptions": {"sol_price_usd": 200.0, "token_usd_price": 1.0,
                            "jito_tip_lamports": 0, "land_probability": 1.0},
            "route": {"name": "clob", "hops": [{"name": "x", "expected_out_ratio": 1.05}]},
        }
        res = cg.run_config(cfg)
        self.assertEqual(res["decision"], "GO")
        self.assertAlmostEqual(res["gross_profit"], 100.0 * 0.05, places=6)
        self.assertIsNotNone(res["min_profitable_size"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
