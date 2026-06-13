"""End-to-end: the exact production poll path against a controlled market.
Calm history → idiosyncratic dip → DIP card with literal ratio → shadow order
fills on trade-through → target hit → prediction graded → calibration moves →
paper resting order fills → position → SELL exit card."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant import store
from quant.config import ADVANCED_DEFAULTS, PRESETS
from quant.engine import poll
from quant.score import calib_default

STABLE = {"Alpha Orb": 50.0, "Beta Orb": 25.0, "Gamma Orb": 60.0,
          "Delta Orb": 40.0, "Epsilon Orb": 30.0, "Zeta Orb": 45.0}


class FakeIO:
    def __init__(self, start):
        self.t = start
        self.test_px = 100.0

    def now_iso(self):
        return self.t.isoformat(timespec="seconds")

    def now(self):
        return self.t.timestamp()

    def sleep(self, s):
        pass

    def step(self, hours):
        self.t += timedelta(hours=hours)

    def ninja(self, league, typ):
        px = {"Divine Orb": 400.0, "Exalted Orb": 1.0, "Chaos Orb": 0.55,
              "Test Orb": self.test_px, **STABLE}
        vol = {"Divine Orb": 5000.0, "Test Orb": 2000.0} | {k: 1500.0 for k in STABLE}
        return {"price_ex": px, "trend": {}, "vol_div": vol, "ex_per_div": 400.0}

    def leagues(self, force=False):
        return [{"Value": "TestLeague", "ShortName": "test", "IsCurrent": True,
                 "DivinePrice": 400.0}]

    def pairs(self, league, rate):
        return {}, None


def make_cfg():
    return {"league": "TestLeague", "mode": "paper", "risk": "conservative", "pins": [],
            "adv": {**ADVANCED_DEFAULTS, "scan_types": ["Currency"], "recipes": [],
                    "paper_bankroll_div": 20.0},
            "preset": PRESETS["conservative"]}


def dip_script():
    """An item that has DEMONSTRATED reversion (three dip-and-recover cycles),
    then dips again — only such items deserve a confident forecast."""
    calm = lambda n: [100.0 + (0.3 if i % 2 else -0.3) for i in range(n)]
    cycle = lambda lo: [lo, lo + 1.5, lo + 3.0, 100.3]
    return (calm(8) + cycle(96.0) + calm(4) + cycle(95.5) + calm(4)
            + cycle(96.5) + calm(4) + [95.0, 91.5, 89.0, 88.3, 88.5, 88.4, 88.5])


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.db")
        self.cfg = make_cfg()
        self.io = FakeIO(datetime.now(timezone.utc) - timedelta(hours=48))

    def tearDown(self):
        self.tmp.cleanup()

    def run_poll(self):
        return poll(self.cfg, self.io, db_path=self.db)

    def play_script(self, prices):
        snap = None
        for px in prices:
            self.io.test_px = px
            snap = self.run_poll()
            self.io.step(1)
        return snap

    def test_first_poll_is_no_trade_and_complete(self):
        snap = self.run_poll()
        self.assertTrue(all(c["act"] in ("HOLD",) for c in snap["cards"]) or not snap["cards"])
        self.assertIsNotNone(snap["no_trade"])
        self.assertIn("divines", snap["no_trade"]["line"])
        self.assertTrue(snap["trust"])
        self.assertIn(snap["grad"]["ready"], (True, False))
        # always-on status strip — an empty board is never a mystery
        self.assertIn("status", snap)
        self.assertEqual(snap["status"]["positions"], 0)
        self.assertEqual(snap["status"]["scanned"], snap["stats"]["scanned"])
        c = store.connect(self.db)
        self.assertIsNotNone(store.kv_json(c, "last_snap"))
        c.close()

    def test_no_capital_explains_itself(self):
        # the heart of the user's confusion: with almost no liquid capital the
        # board is empty — but the status now says exactly why, never silent
        self.cfg["mode"] = "real"
        c = store.connect(self.db)
        store.append(c, "holdings_set", {"div": 0, "ex": 2, "chaos": 0})
        c.commit()
        c.close()
        snap = self.play_script(dip_script())
        entries = [c for c in snap["cards"] if c["act"] in ("DIP", "MAKE", "ROUTE")]
        self.assertFalse(entries)
        self.assertIsNotNone(snap["status"]["entries_reason"])

    def test_full_cycle(self):
        snap = self.play_script(dip_script())
        dips = [c for c in snap["cards"] if c["act"] == "DIP"]
        self.assertEqual(len(dips), 1, f"expected a DIP card, got {snap['cards']}\n"
                                       f"scan={snap['scan']}\nmiss={snap['no_trade']}")
        card = dips[0]
        self.assertEqual(card["item"], "Test Orb")
        self.assertIn("set", card["head"])           # literal in-game ratio
        self.assertIn("in 10", card["plan"])         # plain odds, no decimals
        self.assertLess(card["px"], 89.0)
        entry = card["px"]
        target = card["target_px"]
        self.assertLess(target, 100.0)
        c = store.connect(self.db)
        shadow = store.kv_json(c, "shadow")
        self.assertEqual(len(shadow["orders"]), 1)
        # a paper take = resting order, honest fills only (use the card's numbers)
        oid = store.append(c, "order", {"ledger": "paper", "item": "Test Orb",
                                        "side": "buy", "qty": card["qty"], "px": entry,
                                        "card_id": card["id"], "sig": "DIP",
                                        "target_px": target},
                           ts=self.io.now_iso())
        c.commit()
        c.close()
        # price trades through the entry → both shadow and paper order fill
        self.io.step(0.5)
        self.io.test_px = entry - 1.0
        self.run_poll()
        self.io.step(1)
        c = store.connect(self.db)
        shadow = store.kv_json(c, "shadow")
        self.assertEqual(len(shadow["orders"]), 0)
        self.assertEqual(len(shadow["pos"]), 1)
        fills = store.fills(c, "paper")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["order_id"], oid)
        pos = store.positions(c, "paper")
        self.assertAlmostEqual(pos["Test Orb"]["qty"], card["qty"])
        c.close()
        # price recovers through the target → shadow closes, prediction graded,
        # and the held position turns into a SELL exit card
        self.io.test_px = target + 2.0
        snap = self.run_poll()
        c = store.connect(self.db)
        graded = store.predictions_graded(c, 30)
        self.assertEqual(len(graded), 1)
        self.assertEqual(graded[0]["out"]["hit"], 1)
        self.assertGreater(graded[0]["out"]["realized_pct"], 0)
        calib = store.kv_json(c, "calib")
        base = calib_default(self.cfg["adv"])
        self.assertEqual(calib["DIP"]["hit"][0], base["DIP"]["hit"][0] + 1)
        c.close()
        sells = [c_ for c_ in snap["cards"] if c_["act"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["item"], "Test Orb")
        self.assertIn("thesis", sells[0]["plan"])
        # no fresh entry card for an item already held
        self.assertFalse([c_ for c_ in snap["cards"]
                          if c_["act"] == "DIP" and c_["item"] == "Test Orb"])

    def test_league_history_makes_day_zero_card(self):
        # seed the daily table the way --bootstrap would, then dip on poll #1:
        # no intraday history exists, yet the league anchor carries the signal
        c = store.connect(self.db)
        for i in range(20):
            d = f"2026-05-{i + 1:02d}"
            store.daily_upsert(c, "Test Orb", d, 100.0 + (0.4 if i % 2 else -0.4), 5000)
            for nm, px in STABLE.items():
                store.daily_upsert(c, nm, d, px, 3000)
        c.commit()
        c.close()
        self.io.test_px = 88.0
        snap = self.run_poll()
        dips = [c_ for c_ in snap["cards"] if c_["act"] == "DIP"]
        self.assertEqual(len(dips), 1, f"{snap['cards']} / {snap['no_trade']}")
        self.assertIn("league-history", dips[0]["why"])
        self.assertIn("set", dips[0]["head"])

    def test_unfilled_entry_expires_and_grades_fill_forecast(self):
        snap = self.play_script(dip_script())
        self.assertTrue([c for c in snap["cards"] if c["act"] == "DIP"])
        # price runs away instead of filling; past the fill window the order expires
        self.io.test_px = 99.0
        for _ in range(8):
            self.run_poll()
            self.io.step(1)
        c = store.connect(self.db)
        graded = store.predictions_graded(c, 30)
        self.assertTrue(graded)
        self.assertEqual(graded[0]["out"]["filled"], 0)
        shadow = store.kv_json(c, "shadow")
        self.assertEqual(shadow["orders"], [])
        c.close()


if __name__ == "__main__":
    unittest.main()
