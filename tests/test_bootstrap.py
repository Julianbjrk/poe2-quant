"""The league-history bootstrap: measured priors in, guessed priors out —
and never stomping on live evidence."""
import tempfile
import unittest
from pathlib import Path

from quant import store
from quant.bootstrap import apply_priors, walk_daily
from quant.config import ADVANCED_DEFAULTS as ADV


def series(vals):
    return [(f"d{i:03d}", v, 100.0) for i, v in enumerate(vals)]


def universe(extra):
    out = {f"Stable {i}": series([50.0] * 40) for i in range(6)}
    out.update(extra)
    return out


class TestWalk(unittest.TestCase):
    def test_recovering_dips_measure_high(self):
        vals = [100.0] * 40
        for d in (15, 22, 29):
            vals[d] = 90.0
        res = walk_daily(universe({"Reverter": series(vals)}), ADV)
        self.assertGreaterEqual(res["events"], 3)
        self.assertEqual(res["hit_rate"], 1.0)
        self.assertGreaterEqual(res["rev_mean"], 0.9)

    def test_dead_dips_measure_low(self):
        vals = [100.0] * 20 + [90.0] * 20
        rec = [100.0] * 40
        for d in (15, 22, 29):
            rec[d] = 90.0
        res = walk_daily(universe({"Faller": series(vals), "Reverter": series(rec)}), ADV)
        self.assertGreaterEqual(res["events"], 5)
        self.assertLess(res["hit_rate"], 1.0)
        self.assertGreater(res["hit_rate"], 0.0)
        self.assertLess(res["rev_mean"], 1.0)

    def test_marketwide_dip_is_not_an_event(self):
        # everything dips together → idio ≈ 0 → no events (that's the circuit
        # breaker's territory, not DIP's)
        vals = [100.0] * 40
        vals[20] = 90.0
        crowd = {f"Item {i}": series([v / 2 for v in vals]) for i in range(6)}
        res = walk_daily(universe({"Item X": series(vals), **crowd}), ADV)
        self.assertEqual(res["events"], 0)


class TestWalkTrend(unittest.TestCase):
    def test_persistent_divine_uptrend_measures_high(self):
        from quant.bootstrap import walk_trend
        # div/ex 165 → ~940 over the league: a strong, persistent denomination run
        vals = [165.0 * (1.062 ** i) for i in range(30)]
        res = walk_trend(universe({"Divine Orb": series(vals)}), ADV)
        self.assertGreater(res["events"], 10)
        self.assertGreaterEqual(res["hit_rate"], 0.9)

    def test_flat_divine_has_no_trend_events(self):
        from quant.bootstrap import walk_trend
        res = walk_trend(universe({"Divine Orb": series([300.0] * 30)}), ADV)
        self.assertEqual(res["events"], 0)
        self.assertIsNone(res["hit_rate"])


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = store.connect(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_measured_priors_are_written_capped(self):
        res = {"events": 40, "hits": 28, "hit_rate": 0.7, "rev_mean": 0.85,
               "items": 5, "sample": []}
        self.assertTrue(apply_priors(self.c, res, ADV, log=lambda *a: None))
        cal = store.kv_json(self.c, "calib")
        a, b = cal["DIP"]["hit"]
        self.assertAlmostEqual(a / (a + b), 0.7, delta=0.05)
        self.assertGreater(b, 0)                 # Laplace: no degenerate certainty
        self.assertLessEqual(a + b, 32)          # pseudo-n cap holds
        self.assertAlmostEqual(cal["DIP"]["rev"][0], 0.85)

    def test_live_evidence_is_never_stomped(self):
        store.predict_write(self.c, "p1", "c1", "DIP", "X", {"p_hit": 0.6})
        store.predict_grade(self.c, "p1", {"filled": 1, "hit": 1, "realized_pct": 4.0})
        res = {"events": 40, "hits": 28, "hit_rate": 0.7, "rev_mean": 0.85,
               "items": 5, "sample": []}
        self.assertFalse(apply_priors(self.c, res, ADV, log=lambda *a: None))
        self.assertIsNone(store.kv_json(self.c, "calib"))       # untouched
        self.assertIsNotNone(store.kv_json(self.c, "calib_boot"))  # reference kept


if __name__ == "__main__":
    unittest.main()
