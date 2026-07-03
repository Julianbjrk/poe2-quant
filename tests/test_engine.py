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

    def test_unscannable_position_makes_a_closeable_check_card(self):
        # a held item the scanner can't price (e.g. a catalyst) must give the
        # user a way to close it instead of a permanent dead card
        from quant.engine import exit_card
        st = {"qty": 3, "avg": 1.08, "cost_ex": 3.24, "target_px": None, "sig": None}
        card = exit_card("Uul-Netol's Catalyst", st, None, self.cfg["adv"], 400.0)
        self.assertEqual(card["act"], "CHECK")
        self.assertTrue(card["closeable"])
        self.assertEqual(card["item"], "Uul-Netol's Catalyst")
        self.assertEqual(card["qty"], 3)

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
        self.assertGreaterEqual(len([o for o in shadow["orders"]
                                     if o["item"] == "Test Orb" and o["sig"] == "DIP"]), 1)
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
        self.assertFalse([o for o in shadow["orders"] if o["item"] == "Test Orb" and o["sig"] == "DIP"])
        self.assertGreaterEqual(len([p for p in shadow["pos"]
                                     if p["item"] == "Test Orb" and p["sig"] == "DIP"]), 1)
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
        dip_hits = [g for g in graded if g["item"] == "Test Orb" and g["sig"] == "DIP"
                    and g["out"].get("hit") == 1]
        self.assertTrue(dip_hits)
        self.assertGreater(dip_hits[0]["out"]["realized_pct"], 0)
        calib = store.kv_json(c, "calib")
        base = calib_default(self.cfg["adv"])
        self.assertGreaterEqual(calib["DIP"]["hit"][0], base["DIP"]["hit"][0] + 1)
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

    def test_predictions_carry_p_model(self):
        import quant.engine as eng
        self.play_script(dip_script())
        c = store.connect(self.db)
        rows = c.execute("SELECT payload FROM predictions").fetchall()
        c.close()
        self.assertTrue(rows)
        import json
        pl = json.loads(rows[0][0])
        self.assertIn("p_hit", pl)
        self.assertIn("p_model", pl)              # the reliability diagnostic
        self.assertEqual(pl["model"], eng.MODEL_V)

    def test_shadow_grades_at_horizon_with_mfe(self):
        # a position that never touches its target grades MISS at H_h (not the
        # 96h backstop), and the reversion is measured from max favorable
        # excursion within the horizon — not terminal mark-to-last.
        import json
        import quant.engine as eng
        from quant.engine import shadow_process
        c = store.connect(self.db)
        cache = {}
        t0 = datetime(2026, 6, 10, tzinfo=timezone.utc)
        iso = lambda h: (t0 + timedelta(hours=h)).isoformat(timespec="seconds")
        for h, px in [(0, 100.), (1, 101.), (2, 104.), (3, 102.), (4, 101.), (5, 100.5), (6, 100.)]:
            store.insert_ticks(c, iso(h), [("Zed Orb", "ninja", px, 100)], cache)
        store.predict_write(c, "p:z", "z", "DIP", "Zed Orb",
                            {"p_hit": 0.6, "p_model": 0.6, "H_h": 6.0, "gap_pct": 10.0,
                             "entry": 100.0, "target": 110.0, "model": eng.MODEL_V})
        c.commit()
        shadow = {"orders": [], "pos": [{"pid": "p:z", "sig": "DIP", "item": "Zed Orb",
                  "px": 100.0, "target": 110.0, "qty": 1, "H_h": 6.0, "entry_ts": iso(0)}]}
        calib = calib_default(self.cfg["adv"])
        shadow_process(c, shadow, iso(5), self.cfg["adv"], calib)   # within H_h → still open
        self.assertEqual(len(shadow["pos"]), 1)
        shadow_process(c, shadow, iso(7), self.cfg["adv"], calib)   # past H_h → graded miss
        self.assertEqual(shadow["pos"], [])
        o = json.loads(c.execute("SELECT outcome FROM predictions WHERE id='p:z'").fetchone()[0])
        self.assertEqual(o["hit"], 0)
        self.assertAlmostEqual(o["mfe_pct"], 4.0, delta=0.5)        # 104/100-1 = +4%
        c.close()

    def test_model_bump_resets_and_guards_calibration(self):
        # the migration: a forecast-math change (MODEL_V) must archive + reset the
        # posteriors and gates, re-seed DIP from the last bootstrap, and NOT let
        # an old-definition outcome graded during the bump poll contaminate them.
        import quant.engine as eng
        OLD = "m_pre"
        c = store.connect(self.db)
        old_calib = calib_default(self.cfg["adv"])
        old_calib["DIP"]["hit"] = [20.0, 5.0]
        store.kv_set_json(c, "calib", old_calib)
        store.kv_set(c, "calib_model", OLD)
        store.kv_set_json(c, "gates", {"MAKE": {"off": True, "n": 30}})
        store.kv_set_json(c, "calib_boot", {"events": 40, "hit_rate": 0.7, "rev_mean": 0.85})
        store.predict_write(c, "p:old", "old", "DIP", "Alpha Orb",
                            {"p_hit": 0.6, "p_model": 0.6, "H_h": 6.0, "gap_pct": 5.0,
                             "entry": 50.0, "target": 99.0, "model": OLD})
        stale = (self.io.t - timedelta(hours=99)).isoformat(timespec="seconds")
        store.kv_set_json(c, "shadow", {"orders": [], "pos": [
            {"pid": "p:old", "sig": "DIP", "item": "Alpha Orb", "px": 50.0,
             "target": 99.0, "qty": 1, "H_h": 6.0, "entry_ts": stale}]})
        c.commit()
        c.close()
        self.assertNotEqual(eng.MODEL_V, OLD)
        self.run_poll()                                  # triggers the migration
        c = store.connect(self.db)
        self.assertEqual(store.kv_get(c, "calib_model"), eng.MODEL_V)
        self.assertEqual(store.kv_json(c, "calib_archive:" + OLD)["DIP"]["hit"], [20.0, 5.0])
        self.assertEqual(store.kv_json(c, "gates"), {})
        calib = store.kv_json(c, "calib")
        # DIP re-seeded from calib_boot (0.7), NOT the archived [20,5] and NOT
        # bumped by the m1 outcome graded this poll (contamination guard).
        self.assertAlmostEqual(calib["DIP"]["hit"][0] / sum(calib["DIP"]["hit"]), 0.7, delta=0.07)
        self.assertAlmostEqual(sum(calib["DIP"]["hit"]), 22.0, delta=0.5)  # 20 pseudo + Laplace
        closed = c.execute("SELECT outcome FROM predictions WHERE id='p:old'").fetchone()[0]
        self.assertIsNotNone(closed)                     # row still closed
        c.close()

    def test_shadow_quota_prevents_signal_monopoly(self):
        # a loud signal must not eat the whole shadow-learning budget: 20 open
        # ROUTE forecasts must still leave room for a DIP forecast to be graded.
        self.play_script(dip_script())
        c = store.connect(self.db)
        flood = [{"pid": f"p:R{i}", "card_id": f"R{i}", "sig": "ROUTE", "item": f"Route {i}",
                  "px": 10.0, "target": 12.0, "qty": 1, "H_h": 12.0, "ts": self.io.now_iso()}
                 for i in range(20)]
        store.kv_set_json(c, "shadow", {"orders": flood, "pos": []})
        store.kv_set_json(c, "cards_active", [])
        c.commit()
        c.close()
        self.io.test_px = 88.5
        self.run_poll()
        c = store.connect(self.db)
        sh = store.kv_json(c, "shadow")
        dip_shadow = ([o for o in sh["orders"] if o["sig"] == "DIP"]
                      + [p for p in sh["pos"] if p["sig"] == "DIP"])
        self.assertTrue(dip_shadow, "DIP starved despite the per-signal shadow quota")
        c.close()

    def test_shadow_book_forecasts_even_when_slots_full(self):
        # the bug the user hit: with all position slots full, the shadow book
        # must KEEP forecasting the top opportunities so self-grading never stalls.
        self.play_script(dip_script())
        c = store.connect(self.db)
        for item, px in [("Alpha Orb", 50.), ("Beta Orb", 25.), ("Gamma Orb", 60.)]:
            store.append(c, "fill", {"ledger": "paper", "item": item, "side": "buy", "qty": 1, "px": px})
        store.kv_set_json(c, "shadow", {"orders": [], "pos": []})   # start the shadow book empty
        store.kv_set_json(c, "cards_active", [])
        c.commit()
        c.close()
        self.io.test_px = 88.5
        snap = self.run_poll()
        self.assertGreaterEqual(snap["status"]["positions"], 3)
        self.assertEqual(snap["status"]["slots_free"], 0)         # no room to act
        self.assertGreater(snap["stats"]["shadow_open"], 0)       # but still forecasting
        c = store.connect(self.db)
        sh = store.kv_json(c, "shadow")
        self.assertGreater(len(sh["orders"]) + len(sh["pos"]), 0)
        c.close()

    def test_resting_orders_count_against_slots(self):
        # resting paper orders are pending commitments — they fill the cap so
        # cards can't pile up unfilled bids (the over-commitment the user hit).
        self.play_script(dip_script())
        c = store.connect(self.db)
        store.kv_set_json(c, "cards_active", [])
        for i in range(3):
            store.append(c, "order", {"ledger": "paper", "item": f"Pending {i}",
                                      "side": "buy", "qty": 2, "px": 20})
        c.commit()
        c.close()
        self.io.test_px = 88.5
        snap = self.run_poll()
        self.assertEqual(snap["status"]["orders"], 3)
        self.assertEqual(snap["status"]["slots_free"], 0)
        self.assertFalse([c_ for c_ in snap["cards"] if c_["act"] in ("DIP", "MAKE", "ROUTE", "PARITY")])
        self.assertIn("slots", snap["status"]["entries_reason"])

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
        dip_to = [g for g in graded if g["item"] == "Test Orb" and g["sig"] == "DIP"]
        self.assertTrue(dip_to)
        self.assertTrue(any(g["out"]["filled"] == 0 for g in dip_to))  # a bid that never reached
        shadow = store.kv_json(c, "shadow")
        self.assertFalse([o for o in shadow["orders"]
                          if o["item"] == "Test Orb" and o["sig"] == "DIP"])  # its DIP order is gone
        c.close()


if __name__ == "__main__":
    unittest.main()
