import math
import unittest

from quant.config import ADVANCED_DEFAULTS as ADV
from quant.score import (brier, calib_apply, calib_default, crps_gauss,
                         graduation, model_reliability, normal_update,
                         summarize, update_gates)


class TestScores(unittest.TestCase):
    def test_brier(self):
        self.assertAlmostEqual(brier([(0.8, 1), (0.8, 0)]), (0.04 + 0.64) / 2)

    def test_crps_gauss_at_center(self):
        # closed form at y = mu: sd * (2φ(0) − 1/√π)
        expect = 1.0 * (2 / math.sqrt(2 * math.pi) - 1 / math.sqrt(math.pi))
        self.assertAlmostEqual(crps_gauss(0, 0, 1), expect, places=6)
        self.assertGreater(crps_gauss(3, 0, 1), crps_gauss(0.5, 0, 1))


class TestCalibration(unittest.TestCase):
    def test_outcomes_move_the_posterior(self):
        cal = calib_default(ADV)
        p0 = cal["DIP"]["hit"][0] / sum(cal["DIP"]["hit"])
        pred = {"gap_pct": 10.0}
        for _ in range(10):
            calib_apply(cal, "DIP", pred, {"filled": 1, "hit": 1, "realized_pct": 8.0})
        p1 = cal["DIP"]["hit"][0] / sum(cal["DIP"]["hit"])
        self.assertGreater(p1, p0)
        self.assertGreater(cal["DIP"]["rev"][0], 0.7)  # realized 0.8 pulls prior up

    def test_unfilled_only_touches_fill_posterior(self):
        cal = calib_default(ADV)
        hit_before = list(cal["DIP"]["hit"])
        calib_apply(cal, "DIP", {}, {"filled": 0})
        self.assertEqual(cal["DIP"]["hit"], hit_before)

    def test_rev_prefers_mfe_over_realized(self):
        # a non-hit that nonetheless reverted a lot (high MFE) must still lift the
        # reversion fraction — the bug the shorter horizon would otherwise cause.
        cal = calib_default(ADV)
        pred = {"gap_pct": 10.0}
        for _ in range(8):
            calib_apply(cal, "DIP", pred,
                        {"filled": 1, "hit": 0, "realized_pct": -2.0, "mfe_pct": 9.0})
        self.assertGreater(cal["DIP"]["rev"][0], 0.75)   # MFE 9/10 pulls it up


class TestWelford(unittest.TestCase):
    def test_variance_reflects_dispersion_not_decay(self):
        # noisy data must keep the variance HIGH (the old *0.98 decay collapsed it
        # toward the floor regardless of the data — false confidence).
        msn = [0.5, 0.04, 12.0, 0.04 * 12]
        for i in range(60):
            normal_update(msn, 0.5 + (0.4 if i % 2 else -0.4))   # ±0.4 alternating
        self.assertGreater(msn[1], 0.05)                  # variance stays near 0.16, not ~0

    def test_n_caps(self):
        msn = [0.7, 0.02, 12.0, 0.24]
        for _ in range(500):
            normal_update(msn, 0.7, n_cap=200.0)
        self.assertLessEqual(msn[2], 200.0)

    def test_tolerates_legacy_triple(self):
        msn = [0.7, 0.02, 12.0]            # old 3-tuple
        normal_update(msn, 0.9)
        self.assertEqual(len(msn), 4)      # M2 seeded in place


class TestReliability(unittest.TestCase):
    def test_buckets_model_prob_vs_realized(self):
        graded = []
        for p, y in [(0.3, 0), (0.35, 0), (0.7, 1), (0.75, 1), (0.85, 1)]:
            graded.append({"sig": "DIP", "pred": {"p_model": p},
                           "out": {"filled": 1, "hit": y}})
        rel = model_reliability(graded)
        self.assertEqual(rel["DIP"]["n"], 5)
        # low-p_model bucket should show low realized freq, high bucket high freq
        lo = [b for b in rel["DIP"]["buckets"] if b["hi"] <= 0.4][0]
        hi = [b for b in rel["DIP"]["buckets"] if b["lo"] >= 0.6][0]
        self.assertLess(lo["freq"], hi["freq"])


def _graded(sig, n, edge):
    return [{"sig": sig, "pred": {"p_fill": 0.8, "p_hit": 0.6, "ret_mu": edge, "ret_sd": 4},
             "out": {"filled": 1, "hit": edge > 0, "realized_pct": edge + (i % 3 - 1)}}
            for i in range(n)]


class TestGates(unittest.TestCase):
    def test_negative_edge_gates_off_positive_comes_back(self):
        gates = {}
        update_gates(gates, summarize(_graded("MAKE", 30, -2.0)), ADV)
        self.assertTrue(gates["MAKE"]["off"])
        update_gates(gates, summarize(_graded("MAKE", 30, +2.0)), ADV)
        self.assertFalse(gates["MAKE"]["off"])

    def test_small_samples_never_gate(self):
        gates = {}
        update_gates(gates, summarize(_graded("DIP", 5, -3.0)), ADV)
        self.assertNotIn("DIP", gates)

    def test_hit_deficit_gates_a_rarely_closing_signal(self):
        # ROUTE's real failure: only 14 closed (the edge gate needs 20) yet 0/14
        # hit vs a promised ~60% — the hit-calibration gate must catch it anyway.
        gates = {}
        update_gates(gates, summarize(_graded("ROUTE", 14, -0.2)), ADV)
        self.assertTrue(gates["ROUTE"]["off"])

    def test_overdelivering_signal_not_gated(self):
        # a signal that hits MORE often than it predicted is never gated
        gates = {}
        update_gates(gates, summarize(_graded("DIP", 14, +3.0)), ADV)
        self.assertFalse(gates.get("DIP", {}).get("off", False))


class TestGraduation(unittest.TestCase):
    def test_needs_days_and_tstat(self):
        pts = [{"d": f"2026-06-{i:02d}", "alpha": 0.1 * i} for i in range(1, 20)]
        g = graduation(pts, ADV, "paper")
        self.assertTrue(g["ready"])
        pts = [{"d": f"2026-06-{i:02d}", "alpha": 0.05 * (i % 2)} for i in range(1, 20)]
        g = graduation(pts, ADV, "paper")
        self.assertFalse(g["ready"])
        self.assertIn("not yet", g["line"])


if __name__ == "__main__":
    unittest.main()
