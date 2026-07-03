import math
import unittest

from quant.config import ADVANCED_DEFAULTS as ADV
from quant.score import beta_mean, calib_default
from quant.signals import (basket, dip, fill_blend, make, momo, parity,
                           propose_all, route, tide)

CAL = calib_default(ADV)


def row(px=88.5, theta=100.0, drift_z=0.0, idio_z=-3.0, fam_z=0.0, vol=2000,
        sd_st=0.04, n=100):
    return {"item": "Test Orb", "family": "Currency", "px": px, "vol_div": vol,
            "lvl": math.log(px), "lvl_ex": px, "sd": 0.005, "drift_z": drift_z,
            "sig_h": 0.01, "ou": {"theta": math.log(theta), "b": 0.9,
                                  "sd_st": sd_st, "sig_h": 0.012, "n": n},
            "m24": None, "sd24": None, "n24": 0, "idio_z": idio_z, "fam_z": fam_z}


class TestDip(unittest.TestCase):
    def test_fires_on_idiosyncratic_dip(self):
        p = dip(row(), CAL, ADV)
        self.assertIsNotNone(p)
        self.assertEqual(p["sig"], "DIP")
        self.assertGreater(p["ev_pct"], 0)
        self.assertLess(p["entry_px"], p["target_px"])
        self.assertLess(p["target_px"], 100.0)  # thesis target, shaded under the mean

    def test_knife_guard(self):
        self.assertIsNone(dip(row(drift_z=-2.0), CAL, ADV))

    def test_family_dip_is_not_a_signal(self):
        self.assertIsNone(dip(row(idio_z=-0.2), CAL, ADV))

    def test_family_freefall_blocks(self):
        self.assertIsNone(dip(row(fam_z=-3.0), CAL, ADV))

    def test_no_dip_no_card(self):
        self.assertIsNone(dip(row(px=99.5, idio_z=-0.1), CAL, ADV))

    def test_league_history_fallback_before_intraday(self):
        r = row()
        r["ou"] = None
        r["d14"] = {"theta": math.log(100), "sd_st": 0.015, "n": 14}
        p = dip(r, CAL, ADV)
        self.assertIsNotNone(p)
        self.assertIn("league-history", p["why"])
        self.assertEqual(p["H_h"], 72.0)
        self.assertLess(p["target_px"], 100.0)

    def test_fill_prob_falls_with_distance_to_entry(self):
        # entry derives from the LATENT level; fill is graded as ninja crossing
        # entry within the window. A raw price sitting 3% above entry must show a
        # materially lower p_fill than one sitting at entry (same latent state).
        at_entry = dip(row(px=88.5), CAL, ADV)
        far = dip(dict(row(px=88.5), px=88.5 * 1.03), CAL, ADV)  # raw px up, lvl fixed
        self.assertIsNotNone(at_entry)
        self.assertIsNotNone(far)
        self.assertLess(far["p_fill"], at_entry["p_fill"])


class TestMake(unittest.TestCase):
    def test_deep_book_quotes_around_latent(self):
        p = make(row(px=100, theta=100), CAL, ADV)
        self.assertIsNotNone(p)
        self.assertLess(p["entry_px"], 100)
        self.assertGreater(p["target_px"], 100)

    def test_thin_book_rejected(self):
        self.assertIsNone(make(row(vol=300), CAL, ADV))


class TestRoute(unittest.TestCase):
    RTS = {"exalted": {"px_ex": 100.0, "trades": 60, "value_ex": 6000},
           "divine": {"px_ex": 112.0, "trades": 40, "value_ex": 4480}}

    def test_divergence_becomes_route(self):
        p = route("Test Orb", self.RTS, row(px=100, theta=100), CAL, ADV)
        self.assertIsNotNone(p)
        self.assertTrue(p["deterministic"])
        self.assertEqual(p["det"]["buy_via"], "exalted")
        self.assertGreater(p["gain_pct"], 0)

    def test_band_guard_kills_lot_size_distortion(self):
        rts = {"exalted": {"px_ex": 100.0, "trades": 60, "value_ex": 6000},
               "divine": {"px_ex": 900.0, "trades": 40, "value_ex": 4480}}
        self.assertIsNone(route("Test Orb", rts, row(px=100, theta=100), CAL, ADV))

    def test_thin_legs_rejected(self):
        rts = {"exalted": {"px_ex": 100.0, "trades": 2, "value_ex": 200},
               "divine": {"px_ex": 112.0, "trades": 40, "value_ex": 4480}}
        self.assertIsNone(route("Test Orb", rts, row(px=100, theta=100), CAL, ADV))

    def test_phantom_route_on_unscanned_item_is_rejected(self):
        # the real bug: a poe2scout-only item (no ninja row) with a lot-distorted
        # chaos book gives a fake +273% gap. The exalted-anchor guard now runs
        # even with row=None, so the distorted leg is dropped and no route fires.
        rts = {"exalted": {"px_ex": 34.0, "trades": 38, "value_ex": 1292},
               "chaos": {"px_ex": 127.0, "trades": 38, "value_ex": 4826}}
        self.assertIsNone(route("Adaptive Alloy", rts, None, CAL, ADV))

    def test_implausible_gap_capped(self):
        # a gap within the outlier band but above the ceiling is still too-good-
        # to-be-true (a distorted book, not a real fillable arbitrage)
        rts = {"exalted": {"px_ex": 100.0, "trades": 60, "value_ex": 6000},
               "divine": {"px_ex": 132.0, "trades": 40, "value_ex": 5280}}  # +32% > 25% ceiling
        self.assertIsNone(route("Test Orb", rts, row(px=100, theta=100), CAL, ADV))

    def test_fill_prob_is_evidence_weighted(self):
        # ROUTE quoted p_fill≈0.95 while realising ~3% over 406 forecasts. With its
        # field posterior seeded, the SHOWN p_fill collapses to the measured rate
        # while the raw touch model (a diagnostic) stays optimistic.
        cal = calib_default(ADV)
        cal["ROUTE"]["fill"] = [19.0, 391.0]      # ~5% of quotes actually filled
        p = route("Test Orb", self.RTS, row(px=100, theta=100), cal, ADV)
        self.assertIsNotNone(p)
        self.assertLess(p["p_fill"], 0.1)          # blend follows the ledger
        self.assertGreater(p["p_fill_model"], 0.9)  # touch model unchanged, kept as diagnostic


class TestTide(unittest.TestCase):
    def _div_row(self, drift_z=2.0, trend7=8.0, px=460.0):
        return {"item": "Divine Orb", "px": px, "sig_h": 0.01,
                "drift_z": drift_z, "trend7": trend7, "vol_div": 5000}

    def test_emits_proposal_on_divine_uptrend(self):
        p = tide(self._div_row(), CAL, ADV)
        self.assertIsNotNone(p)
        self.assertEqual(p["sig"], "TIDE")
        self.assertEqual(p["item"], "Divine Orb")
        self.assertGreater(p["target_px"], p["entry_px"])
        self.assertGreater(p["target_px"], self._div_row()["px"])   # sell above current

    def test_no_proposal_without_a_real_uptrend(self):
        self.assertIsNone(tide(self._div_row(drift_z=0.5), CAL, ADV))   # drift below threshold
        self.assertIsNone(tide(self._div_row(trend7=-3.0), CAL, ADV))   # 7d trend negative
        self.assertIsNone(tide(None, CAL, ADV))

    def test_becomes_positive_ev_once_persistence_is_proven(self):
        # thin margin at the prior (entry premium + round-trip fees eat a 5% target);
        # only a proven-persistent trend (high hit posterior) makes it card-worthy
        cal = calib_default(ADV)
        cal["TIDE"]["hit"] = [16.0, 2.0]        # history: div/ex uptrend persists ~89%
        self.assertGreater(tide(self._div_row(), cal, ADV)["ev_pct"], 0)


class TestMomo(unittest.TestCase):
    def _row(self, drift_z=2.5, trend7=12.0, above=True):
        theta = math.log(100)
        lvl = theta + (1.5 if above else -1.5) * 0.04     # above/below its own mean
        return {"item": "Test Orb", "family": "Currency", "px": math.exp(lvl),
                "lvl": lvl, "lvl_ex": math.exp(lvl), "sig_h": 0.02, "drift_z": drift_z,
                "trend7": trend7, "vol_div": 2000,
                "ou": {"theta": theta, "b": 0.9, "sd_st": 0.04, "n": 100}}

    def test_fires_in_bull_on_upward_momentum(self):
        p = momo(self._row(), CAL, ADV, "BULL")
        self.assertIsNotNone(p)
        self.assertEqual(p["sig"], "MOMO")
        self.assertGreater(p["target_px"], p["entry_px"])

    def test_never_fires_outside_bull(self):
        self.assertIsNone(momo(self._row(), CAL, ADV, "CHOP"))
        self.assertIsNone(momo(self._row(), CAL, ADV, "BEAR"))

    def test_requires_price_above_its_mean(self):
        self.assertIsNone(momo(self._row(above=False), CAL, ADV, "BULL"))  # that's DIP territory

    def test_requires_drift_and_trend(self):
        self.assertIsNone(momo(self._row(drift_z=1.0), CAL, ADV, "BULL"))
        self.assertIsNone(momo(self._row(trend7=3.0), CAL, ADV, "BULL"))


class TestBasket(unittest.TestCase):
    def _idx(self, px=105.0):
        return {"item": "__BASKET__", "px": px, "vol_div": 50000,
                "members": {"A": 0.4, "B": 0.35, "C": 0.25}}

    def test_advice_card_in_bull(self):
        p = basket(self._idx(), CAL, ADV, "BULL")
        self.assertIsNotNone(p)
        self.assertEqual(p["sig"], "BASKET")
        self.assertEqual(p["item"], "__BASKET__")
        self.assertTrue(p["advice_only"])
        self.assertGreater(p["target_px"], p["entry_px"])
        self.assertIn("A 40%", p["why"])            # member spread executable by hand

    def test_no_basket_outside_bull(self):
        self.assertIsNone(basket(self._idx(), CAL, ADV, "CHOP"))
        self.assertIsNone(basket(self._idx(), CAL, ADV, "BEAR"))
        self.assertIsNone(basket(None, CAL, ADV, "BULL"))


class TestFillBlend(unittest.TestCase):
    def test_returns_touch_model_at_untouched_prior(self):
        # posterior still at its [7,3] prior -> no evidence -> pure touch model
        self.assertAlmostEqual(fill_blend(0.95, [7.0, 3.0]), 0.95, places=9)
        self.assertAlmostEqual(fill_blend(0.30, [7.0, 3.0]), 0.30, places=9)

    def test_field_evidence_overrides_optimistic_touch(self):
        blended = fill_blend(0.95, [19.0, 391.0])
        self.assertLess(blended, 0.1)
        self.assertGreater(blended, beta_mean([19.0, 391.0]) - 0.02)  # near the measured rate


class TestParity(unittest.TestCase):
    def test_recipe_break_is_an_arb(self):
        px = {"Distilled A": 10.0, "Distilled B": 40.0}
        vol = {"Distilled A": 500, "Distilled B": 500}
        rec = [{"give": [["Distilled A", 3]], "get": [["Distilled B", 1]],
                "note": "combine 3:1", "verified": False}]
        props = parity(rec, px, vol, CAL, ADV)
        self.assertEqual(len(props), 1)
        self.assertLessEqual(props[0]["p_hit"], 0.6)  # unverified stays humble
        self.assertIn("unverified", props[0]["why"])

    def test_fair_price_no_arb(self):
        px = {"Distilled A": 10.0, "Distilled B": 30.5}
        rec = [{"give": [["Distilled A", 3]], "get": [["Distilled B", 1]],
                "note": "combine 3:1", "verified": True}]
        self.assertEqual(parity(rec, px, {}, CAL, ADV), [])


class TestRanking(unittest.TestCase):
    def test_deterministic_outranks_statistical(self):
        rows = {"Test Orb": row()}
        routes = {"Route Orb": TestRoute.RTS}
        props = propose_all(rows, routes, [], CAL, ADV, vol_floor=150)
        self.assertGreaterEqual(len(props), 2)
        self.assertEqual(props[0]["sig"], "ROUTE")


if __name__ == "__main__":
    unittest.main()
