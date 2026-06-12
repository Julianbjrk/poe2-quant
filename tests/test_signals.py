import math
import unittest

from quant.config import ADVANCED_DEFAULTS as ADV
from quant.score import calib_default
from quant.signals import dip, make, parity, propose_all, route

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
