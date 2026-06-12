import math
import random
import unittest

from quant.models import (best_ratio, fee_pct, fit_ou, kf_drift_z, kf_level,
                          kf_new, kf_step, ou_horizon, prob_ge, touch_median_h,
                          touch_prob, weighted_median)


class TestRatio(unittest.TestCase):
    def test_buy_never_exceeds_ceiling(self):
        for px in (0.4, 1.0, 3.5, 7.31, 88.49, 412.0):
            r = best_ratio(px, "buy")
            self.assertIsNotNone(r, px)
            self.assertLessEqual(r["unit"], px + 1e-9)
            self.assertLessEqual(r["err_pct"], 4.0)

    def test_sell_never_below_floor(self):
        for px in (0.4, 1.0, 3.5, 7.31, 88.49):
            r = best_ratio(px, "sell")
            self.assertGreaterEqual(r["unit"], px - 1e-9)

    def test_exact_ratios(self):
        r = best_ratio(3.5, "buy")
        self.assertEqual((r["give"], r["get"]), (7, 2))
        r = best_ratio(1 / 3, "sell")
        self.assertEqual((r["give"], r["get"]), (3, 1))

    def test_too_cheap_is_rejected(self):
        # 0.02 ex cannot be expressed within 4% with lots ≤ 20 — the lot-size guard
        self.assertIsNone(best_ratio(0.02, "buy"))


class TestKalman(unittest.TestCase):
    def test_tracks_level_and_sees_drift(self):
        random.seed(7)
        st = kf_new(math.log(100))
        x = math.log(100)
        for _ in range(60):  # 1%/h downtrend, small noise
            x -= 0.01
            kf_step(st, 1.0, {"ninja": x + random.gauss(0, 0.004)})
        self.assertAlmostEqual(kf_level(st), x, delta=0.03)
        self.assertLess(kf_drift_z(st), -1.0)

    def test_fuses_two_sources(self):
        st = kf_new(math.log(100))
        for _ in range(20):
            kf_step(st, 1.0, {"ninja": math.log(100), "pairex": math.log(102)})
        self.assertTrue(math.log(100) < kf_level(st) < math.log(102))


class TestOU(unittest.TestCase):
    def test_recovers_parameters(self):
        random.seed(3)
        b, theta, sig = 0.9, math.log(100), 0.01
        x, closes = theta, []
        for i in range(300):
            x = theta + b * (x - theta) + random.gauss(0, sig)
            closes.append((f"2026-06-{i // 24 + 1:02d}T{i % 24:02d}:00:00+00:00", math.exp(x)))
        ou = fit_ou(closes)
        self.assertAlmostEqual(ou["b"], b, delta=0.06)
        self.assertAlmostEqual(ou["theta"], theta, delta=0.01)

    def test_horizon_reverts_toward_mean(self):
        ou = {"theta": math.log(100), "b": 0.9, "sd_st": 0.05, "sig_h": 0.02}
        mu, sd = ou_horizon(math.log(88), ou, 24, rev_frac=1.0)
        self.assertGreater(mu, math.log(88))
        self.assertLess(mu, math.log(100) + 1e-9)
        self.assertGreater(prob_ge(mu, sd, math.log(95)), 0.5)

    def test_rev_frac_damps_the_forecast(self):
        ou = {"theta": math.log(100), "b": 0.9, "sd_st": 0.05, "sig_h": 0.02}
        mu_full, _ = ou_horizon(math.log(88), ou, 24, rev_frac=1.0)
        mu_damp, _ = ou_horizon(math.log(88), ou, 24, rev_frac=0.5)
        self.assertLess(mu_damp, mu_full)


class TestFill(unittest.TestCase):
    def test_touch_prob_monotone(self):
        self.assertGreater(touch_prob(0.01, 0.01, 6), touch_prob(0.05, 0.01, 6))
        self.assertGreater(touch_prob(0.02, 0.01, 24), touch_prob(0.02, 0.01, 2))
        self.assertLessEqual(touch_prob(0.0001, 0.01, 24), 0.95)

    def test_touch_median_consistent(self):
        t = touch_median_h(0.02, 0.01)
        self.assertAlmostEqual(touch_prob(0.02, 0.01, t), 0.5, delta=0.02)


class TestFees(unittest.TestCase):
    def test_flat_and_interpolated(self):
        self.assertEqual(fee_pct(50, [[0, 1.0]]), 1.0)
        curve = [[0, 2.0], [100, 1.0]]
        self.assertAlmostEqual(fee_pct(50, curve), 1.5)
        self.assertEqual(fee_pct(500, curve), 1.0)

    def test_weighted_median(self):
        self.assertEqual(weighted_median([(1, 1), (2, 1), (10, 5)]), 10)


if __name__ == "__main__":
    unittest.main()
