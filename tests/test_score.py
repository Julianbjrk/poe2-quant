import math
import unittest

from quant.config import ADVANCED_DEFAULTS as ADV
from quant.score import (brier, calib_apply, calib_default, crps_gauss,
                         graduation, summarize, update_gates)


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
