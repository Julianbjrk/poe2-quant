import math
import unittest

from quant.config import ADVANCED_DEFAULTS as ADV
from quant.score import (beta_mean, brier, calib_apply, calib_default,
                         crps_gauss, decay_calib, feature_reliability,
                         fill_by_hour, graduation, model_reliability,
                         normal_update, summarize, update_gates)


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

    def test_touch_and_mfe_on_unfilled_row_never_move_hit(self):
        # Task 4: an unfilled forecast is graded with a fill-independent price
        # outcome (touch/mfe). That is a DIAGNOSTIC — it moves the fill posterior
        # (a miss) but must never move the hit posterior it isn't conditioned on.
        cal = calib_default(ADV)
        hit_before = list(cal["DIP"]["hit"])
        fill_before = list(cal["DIP"]["fill"])
        calib_apply(cal, "DIP", {"gap_pct": 10.0},
                    {"filled": 0, "touch": 1, "mfe_pct": 9.0})
        self.assertEqual(cal["DIP"]["hit"], hit_before)      # touch/mfe never size
        self.assertNotEqual(cal["DIP"]["fill"], fill_before)  # the fill miss IS learned

    def test_rev_prefers_mfe_over_realized(self):
        # a non-hit that nonetheless reverted a lot (high MFE) must still lift the
        # reversion fraction — the bug the shorter horizon would otherwise cause.
        cal = calib_default(ADV)
        pred = {"gap_pct": 10.0}
        for _ in range(8):
            calib_apply(cal, "DIP", pred,
                        {"filled": 1, "hit": 0, "realized_pct": -2.0, "mfe_pct": 9.0})
        self.assertGreater(cal["DIP"]["rev"][0], 0.75)   # MFE 9/10 pulls it up


class TestDecay(unittest.TestCase):
    def test_one_half_life_moves_halfway_to_prior(self):
        cal = calib_default(ADV)                          # DIP hit prior [6,4]
        cal["DIP"]["hit"] = [20.0, 4.0]
        decay_calib(cal, ADV, ADV["calib_half_life_d"])   # exactly one half-life
        self.assertAlmostEqual(cal["DIP"]["hit"][0], 13.0, places=6)  # halfway 20→6
        self.assertAlmostEqual(cal["DIP"]["hit"][1], 4.0, places=6)   # already at prior

    def test_repeated_decay_converges_to_prior_never_crosses(self):
        cal = calib_default(ADV)
        cal["DIP"]["hit"] = [30.0, 2.0]
        prev = cal["DIP"]["hit"][0]
        for _ in range(60):
            decay_calib(cal, ADV, 7.0)
            self.assertGreaterEqual(cal["DIP"]["hit"][0], 6.0 - 1e-9)  # never below prior a
            self.assertLessEqual(cal["DIP"]["hit"][0], prev + 1e-9)    # monotone toward prior
            prev = cal["DIP"]["hit"][0]
        self.assertAlmostEqual(cal["DIP"]["hit"][0], 6.0, places=2)    # converges to prior
        self.assertAlmostEqual(cal["DIP"]["hit"][1], 4.0, places=2)

    def test_fresh_grade_moves_faster_after_decay(self):
        # heavy decay shrinks the effective sample size, so one new hit grade moves
        # the mean MORE than it would against the un-decayed posterior
        out = {"filled": 1, "hit": 1}
        undecayed = calib_default(ADV)
        undecayed["DIP"]["hit"] = [30.0, 20.0]            # mean 0.6, weight 50
        decayed = calib_default(ADV)
        decayed["DIP"]["hit"] = [30.0, 20.0]
        decay_calib(decayed, ADV, 2 * ADV["calib_half_life_d"])   # two half-lives → weight ~12.5
        m_u0, m_d0 = beta_mean(undecayed["DIP"]["hit"]), beta_mean(decayed["DIP"]["hit"])
        self.assertAlmostEqual(m_u0, m_d0, places=6)     # same mean, only the weight shrank
        calib_apply(undecayed, "DIP", {}, out)
        calib_apply(decayed, "DIP", {}, out)
        self.assertGreater(beta_mean(decayed["DIP"]["hit"]) - m_d0,
                           beta_mean(undecayed["DIP"]["hit"]) - m_u0)


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


class TestFeatureReliability(unittest.TestCase):
    def _dip(self, feat, hit):
        return {"sig": "DIP", "pred": {"feat": feat}, "out": {"filled": 1, "hit": hit}}

    def test_z_ou_separates_and_thin_buckets_suppressed(self):
        graded = []
        for _ in range(6):                       # deep dips (z_ou < -2.5) hit
            graded.append(self._dip({"z_ou": -3.0, "drift_z": 0.0}, 1))
        for _ in range(6):                       # shallow ones (>= -2.5) miss
            graded.append(self._dip({"z_ou": -2.0, "drift_z": 0.0}, 0))
        graded.append(self._dip({"z_ou": -3.0, "drift_z": 1.0}, 1))  # lone drift>0.5
        fr = feature_reliability(graded)
        zb = fr["DIP"]["feats"]["z_ou"]
        lo = [b for b in zb if "<-2.5" in b["label"]][0]
        hi = [b for b in zb if ">=-2.5" in b["label"]][0]
        self.assertEqual(lo["freq"], 1.0)
        self.assertEqual(hi["freq"], 0.0)
        # only the mid drift bucket (n=12) survives; the n=1 >0.5 bucket is dropped
        self.assertTrue(all(">0.5" not in b["label"]
                            for b in fr["DIP"]["feats"]["drift_z"]))

    def test_skips_signal_under_ten_filled(self):
        graded = [self._dip({"z_ou": -3.0}, 1) for _ in range(9)]
        self.assertNotIn("DIP", feature_reliability(graded))


class TestFillByHour(unittest.TestCase):
    def test_bands_and_weekend_split_with_suppression(self):
        graded = []
        for _ in range(6):                       # Fri 02:00 UTC — always fills
            graded.append({"sig": "MAKE", "ts": "2026-07-03T02:00:00",
                           "pred": {"p_fill": 0.8}, "out": {"filled": 1}})
        for _ in range(6):                       # Sat 14:00 UTC — never fills
            graded.append({"sig": "MAKE", "ts": "2026-07-04T14:00:00",
                           "pred": {"p_fill": 0.8}, "out": {"filled": 0}})
        fh = fill_by_hour(graded)
        bands = {b["band"]: b for b in fh["utc_band"]}
        self.assertEqual(bands["00-06"]["fill_freq"], 1.0)
        self.assertEqual(bands["12-18"]["fill_freq"], 0.0)
        self.assertNotIn("06-12", bands)         # empty band suppressed
        days = {b["day"]: b for b in fh["day"]}
        self.assertEqual(days["weekday"]["n"], 6)
        self.assertEqual(days["weekend"]["n"], 6)

    def test_empty_when_too_thin(self):
        graded = [{"sig": "MAKE", "ts": "2026-07-03T02:00:00",
                   "pred": {"p_fill": 0.5}, "out": {"filled": 1}} for _ in range(3)]
        self.assertEqual(fill_by_hour(graded), {})


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

    def test_momo_probation_lifts_after_calibrated_fills(self):
        # MOMO starts gated (probation); once ≥gate_fill_min fills hit at the shown
        # (prior) rate, the hit-calibration branch un-gates it automatically
        gates = {"MOMO": {"off": True}}
        graded = [{"sig": "MOMO",
                   "pred": {"p_fill": 0.9, "p_hit": 0.4, "ret_mu": 0.0, "ret_sd": 4},
                   "out": {"filled": 1, "hit": 1 if i < 3 else 0, "realized_pct": 0.0}}
                  for i in range(8)]              # 3/8 ≈ the promised 0.4
        update_gates(gates, summarize(graded), ADV)
        self.assertFalse(gates["MOMO"]["off"])

    def test_momo_stays_gated_with_too_few_fills(self):
        gates = {"MOMO": {"off": True}}
        graded = [{"sig": "MOMO", "pred": {"p_fill": 0.9, "p_hit": 0.4},
                   "out": {"filled": 1, "hit": 1}} for _ in range(4)]   # < gate_fill_min
        update_gates(gates, summarize(graded), ADV)
        self.assertTrue(gates["MOMO"]["off"])    # not enough evidence to lift probation


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
