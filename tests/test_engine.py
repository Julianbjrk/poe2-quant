"""End-to-end: the exact production poll path against a controlled market.
Calm history → idiosyncratic dip → DIP card with literal ratio → shadow order
fills on trade-through → target hit → prediction graded → calibration moves →
paper resting order fills → position → SELL exit card."""
import math
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
    """A genuinely MEAN-REVERTING item — repeated over/undershoots that snap
    back to ~100 — then a decisive dip that STABILISES near the bottom. Both
    properties matter: visible reversion keeps the fitted AR(1) b realistic (a
    flat or one-directional stretch inflates it toward a random walk, which the
    small-sample bias correction then pushes further, over-shrinking the target
    under the horizon cap); and the stabilisation lets the drift estimate clear
    the falling-knife guard so DIP fires on a real dip, not a knife."""
    revert = [100.0, 97.0, 101.5, 98.5, 101.0, 99.0, 100.5,
              96.0, 99.5, 101.0, 98.0, 100.5, 99.5, 100.2]
    settle = [88.0, 88.3, 87.8]
    return revert * 3 + [96.0, 92.0, 89.0, 88.0] + settle * 2


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
        # price recovers well through the target → shadow closes on the raw tick
        # cross and the prediction is graded a hit; the held position then shows a
        # SELL exit card once the smoothed mark confirms. Prices vary each poll as
        # real ones do — a dead-constant quote is (correctly) skipped as a stale
        # repeat, so it would never move the mark.
        for i in range(3):
            self.io.test_px = 100.0 + (0.3 if i % 2 else -0.3)
            snap = self.run_poll()
            self.io.step(1)
        c = store.connect(self.db)
        graded = store.predictions_graded(c, 30)
        dip_hits = [g for g in graded if g["item"] == "Test Orb" and g["sig"] == "DIP"
                    and g["out"].get("hit") == 1]
        self.assertTrue(dip_hits)
        self.assertGreater(dip_hits[0]["out"]["realized_pct"], 0)
        calib = store.kv_json(c, "calib")
        base = calib_default(self.cfg["adv"])
        # the hit grade added ~one observation; recency decay trims a hair each
        # subsequent hourly poll, so allow for it rather than demanding exactly +1
        self.assertGreaterEqual(calib["DIP"]["hit"][0], base["DIP"]["hit"][0] + 0.9)
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
        feat = pl["feat"]                          # diagnostic freight (Task 2)
        self.assertIn("trend7", feat)              # present even if None
        self.assertIn("vol_div", feat)
        self.assertIn("hour", feat)
        self.assertTrue(0 <= feat["hour"] <= 23)

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

    def _run_unfilled_watch(self, prices):
        # helper: an entry order at px=90 that ninja (always >90) never fills;
        # after fill_window it enters the price-watch and resolves at H_h=12.
        import json
        import quant.engine as eng
        from quant.engine import shadow_process
        c = store.connect(self.db)
        cache = {}
        t0 = datetime(2026, 6, 12, tzinfo=timezone.utc)
        iso = lambda h: (t0 + timedelta(hours=h)).isoformat(timespec="seconds")
        for h, px in prices:
            store.insert_ticks(c, iso(h), [("Yaw Orb", "ninja", px, 100)], cache)
        store.predict_write(c, "p:y", "y", "DIP", "Yaw Orb",
                            {"p_hit": 0.6, "p_model": 0.6, "H_h": 12.0, "gap_pct": 10.0,
                             "entry": 90.0, "target": 110.0, "model": eng.MODEL_V})
        c.commit()
        shadow = {"orders": [{"pid": "p:y", "sig": "DIP", "item": "Yaw Orb", "px": 90.0,
                              "target": 110.0, "qty": 1, "H_h": 12.0, "ts": iso(0)}], "pos": []}
        calib = calib_default(self.cfg["adv"])
        hit_before = list(calib["DIP"]["hit"])
        shadow_process(c, shadow, iso(7), self.cfg["adv"], calib)    # >fill_window → to watch
        self.assertEqual(shadow["orders"], [])
        self.assertEqual(len(shadow["watch"]), 1)
        self.assertIsNone(c.execute("SELECT outcome FROM predictions WHERE id='p:y'").fetchone()[0])
        shadow_process(c, shadow, iso(13), self.cfg["adv"], calib)   # ≥H_h → resolve
        self.assertEqual(shadow["watch"], [])
        out = json.loads(c.execute("SELECT outcome FROM predictions WHERE id='p:y'").fetchone()[0])
        c.close()
        self.assertEqual(out["filled"], 0)
        self.assertEqual(calib["DIP"]["hit"], hit_before)           # touch never moves hit
        return out

    def test_unfilled_watch_grades_touch_when_price_reaches_target(self):
        out = self._run_unfilled_watch([(0, 100.), (2, 105.), (4, 111.), (6, 108.),
                                        (8, 106.), (10, 104.), (12, 103.)])
        self.assertEqual(out["touch"], 1)                           # 111 ≥ 110 within H_h

    def test_unfilled_watch_touch_zero_when_target_never_reached(self):
        out = self._run_unfilled_watch([(0, 100.), (2, 105.), (4, 108.), (6, 107.),
                                        (8, 106.), (10, 104.), (12, 103.)])
        self.assertEqual(out["touch"], 0)                           # never crossed 110

    def test_tide_shadow_order_fills_and_grades_hit(self):
        # a TIDE (hold-divines) order rests just above market, fills on the next
        # tick, and grades a hit when div/ex crosses the +5% target — the same
        # shadow machinery every other signal uses, no grading changes needed
        import json
        import quant.engine as eng
        from quant.engine import shadow_process
        c = store.connect(self.db)
        cache = {}
        t0 = datetime(2026, 6, 15, tzinfo=timezone.utc)
        iso = lambda h: (t0 + timedelta(hours=h)).isoformat(timespec="seconds")
        for h, px in [(0, 460.0), (1, 461.0), (2, 480.0), (3, 490.0)]:  # div/ex climbs
            store.insert_ticks(c, iso(h), [("Divine Orb", "ninja", px, 5000)], cache)
        store.predict_write(c, "p:t", "t", "TIDE", "Divine Orb",
                            {"p_hit": 0.5, "p_model": 0.5, "H_h": 72.0,
                             "entry": 464.6, "target": 483.0, "model": eng.MODEL_V})
        c.commit()
        shadow = {"orders": [{"pid": "p:t", "sig": "TIDE", "item": "Divine Orb",
                  "px": 464.6, "target": 483.0, "qty": 1, "H_h": 72.0, "ts": iso(0)}],
                  "pos": [], "watch": []}
        calib = calib_default(self.cfg["adv"])
        shadow_process(c, shadow, iso(3), self.cfg["adv"], calib)
        out = json.loads(c.execute("SELECT outcome FROM predictions WHERE id='p:t'").fetchone()[0])
        c.close()
        self.assertEqual(out["filled"], 1)     # 461 ≤ 464.6 → the buy fills
        self.assertEqual(out["hit"], 1)        # 490 ≥ 483 target within the horizon

    def test_adding_a_signal_backfills_calib_without_touching_others(self):
        # Phase 2 adds signals to an existing m3 calib: setdefault must add TIDE at
        # its prior and leave every existing posterior exactly as it was
        import quant.engine as eng
        from quant.engine import _load_calib_versioned
        c = store.connect(self.db)
        cal = calib_default(self.cfg["adv"])
        cal["DIP"]["hit"] = [18.0, 5.0]        # accumulated live evidence
        del cal["TIDE"]                        # simulate a calib from before TIDE existed
        store.kv_set_json(c, "calib", cal)
        store.kv_set(c, "calib_model", eng.MODEL_V)
        loaded, _ = _load_calib_versioned(c, self.cfg["adv"])
        c.close()
        self.assertIn("TIDE", loaded)                          # backfilled
        self.assertEqual(loaded["TIDE"]["hit"], [5.0, 5.0])    # at its prior
        self.assertEqual(loaded["DIP"]["hit"], [18.0, 5.0])    # untouched

    def test_momo_starts_on_probation_when_added(self):
        # MOMO (trend chasing) must not fire until it proves calibrated: adding it
        # to an existing calib seeds a gate-off, and the card path suppresses gated
        # signals while the shadow book keeps grading them
        import quant.engine as eng
        from quant.engine import _load_calib_versioned
        c = store.connect(self.db)
        cal = calib_default(self.cfg["adv"])
        del cal["MOMO"]
        store.kv_set_json(c, "calib", cal)
        store.kv_set(c, "calib_model", eng.MODEL_V)
        store.kv_set_json(c, "gates", {})
        _, gates = _load_calib_versioned(c, self.cfg["adv"])
        c.close()
        self.assertTrue(gates.get("MOMO", {}).get("off"))      # gated on arrival

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

    def test_stale_pair_quotes_are_not_re_fed_to_the_filter(self):
        # LiveIO.pairs serves a cached book for ~55 min; the same pair quote must
        # not be re-fed to the Kalman filter every 5-min poll. With the fix an
        # unchanged pair quote is skipped, so the latent state is IDENTICAL to one
        # that never saw the pair at all — while moving ninja obs still flow.
        import quant.engine as eng
        from quant.models import kf_level

        class PairIO(FakeIO):
            book = True

            def pairs(self, league, rate):
                if self.book:
                    return {"Test Orb": {"exalted": {"px_ex": 80.0, "trades": 50,
                                                     "value_ex": 0}}}, None
                return {}, None

        seq = [100.0, 101.0, 100.5, 102.0, 101.5, 103.0, 102.5, 104.0]
        start = datetime.now(timezone.utc) - timedelta(hours=48)
        # run A: a stale exalted book every poll. Pre-seed the cache so the (never
        # changing) quote reads as a repeat from the very first poll → never fed.
        dbA = str(Path(self.tmp.name) / "a.db")
        ioA = PairIO(start)
        eng._tick_cache[dbA] = {("Test Orb", "pairex"): 80.0}
        for px in seq:
            ioA.test_px = px
            poll(self.cfg, ioA, db_path=dbA)
            ioA.step(1)
        # run B: no pair source at all, identical ninja sequence
        dbB = str(Path(self.tmp.name) / "b.db")
        ioB = PairIO(start)
        ioB.book = False
        for px in seq:
            ioB.test_px = px
            poll(self.cfg, ioB, db_path=dbB)
            ioB.step(1)
        cA = store.connect(dbA)
        fA = store.kv_json(cA, "filters")["Test Orb"]
        cA.close()
        cB = store.connect(dbB)
        fB = store.kv_json(cB, "filters")["Test Orb"]
        cB.close()
        self.assertAlmostEqual(fA["P"][0][0], fB["P"][0][0], delta=1e-9)   # stale pair inert
        self.assertEqual(fA["n"], len(seq))                # exactly one obs/poll: ninja only
        self.assertAlmostEqual(kf_level(fA), math.log(seq[-1]), delta=0.5)  # tracked ninja

    def test_regime_index_chain_links_across_weight_change(self):
        # the rolling index must NOT jump when its top-20 membership is refreshed
        # (weekly): weights change but levels are never compared across weight sets
        from quant.engine import _regime_step
        c = store.connect(self.db)
        adv = self.cfg["adv"]
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        iso = lambda h: (t0 + timedelta(hours=h)).isoformat(timespec="seconds")

        def rows_at(prices, vols):
            return {nm: {"item": nm, "vol_div": vols[nm], "lvl": math.log(prices[nm]),
                         "drift_z": 0.0} for nm in prices}
        v1 = {"A": 5000, "B": 4000, "C": 3000}
        _regime_step(c, rows_at({"A": 100.0, "B": 50.0, "C": 25.0}, v1), iso(0), 0.1, 0.02, adv)
        _regime_step(c, rows_at({"A": 102.0, "B": 51.0, "C": 25.2}, v1), iso(1), 1.0, 0.02, adv)
        lvl_before = store.kv_json(c, "regime_idx")["level"]
        self.assertNotAlmostEqual(lvl_before, 1.0)          # the index actually moved
        # >168h later a different set is liquid → weight refresh on this poll
        p3 = {"A": 103.0, "B": 51.5, "C": 25.3, "D": 10.0, "E": 8.0, "F": 6.0}
        v3 = {"A": 100, "B": 100, "C": 100, "D": 9000, "E": 8000, "F": 7000}
        _regime_step(c, rows_at(p3, v3), iso(200), 1.0, 0.02, adv)
        lvl_after = store.kv_json(c, "regime_idx")["level"]
        c.close()
        self.assertAlmostEqual(lvl_after, lvl_before, places=9)   # no jump on the switch

    def test_circuit_breaker_is_class_aware(self):
        # a mania (market far ABOVE anchor) halts mean-reversion but leaves trend
        # signals — which are in their element — free; a crash halts everything
        from quant.engine import _blocked_signals
        mania = _blocked_signals(3.0, 400.0, 2.5)
        self.assertIn("DIP", mania)
        self.assertIn("MAKE", mania)
        self.assertNotIn("MOMO", mania)          # trend stays active
        self.assertNotIn("TIDE", mania)
        self.assertNotIn("BASKET", mania)
        self.assertNotIn("ROUTE", mania)         # ROUTE/PARITY governed by their own gates
        crash = _blocked_signals(-3.0, 400.0, 2.5)
        for sig in ("DIP", "MAKE", "MOMO", "TIDE", "BASKET"):
            self.assertIn(sig, crash)            # everything halted in a crash
        self.assertEqual(_blocked_signals(0.5, 400.0, 2.5), set())      # calm blocks nothing
        self.assertIn("MOMO", _blocked_signals(0.0, None, 2.5))         # no feed halts all

    def test_regime_step_writes_synthetic_basket_tick(self):
        from quant.engine import _regime_step
        c = store.connect(self.db)
        rows = {nm: {"item": nm, "vol_div": v, "lvl": math.log(p), "drift_z": 0.0}
                for nm, p, v in [("A", 100.0, 5000), ("B", 50.0, 4000)]}
        _regime_step(c, rows, "2026-06-01T00:00:00+00:00", 0.1, 0.02, self.cfg["adv"], cache={})
        tick = c.execute("SELECT price_ex, vol_div FROM ticks WHERE item='__BASKET__'").fetchone()
        c.close()
        self.assertIsNotNone(tick)                       # synthetic tick written
        self.assertAlmostEqual(tick[0], 100.0, delta=1.0)  # 100 × index level (~1.0 at start)
        self.assertEqual(tick[1], 9000)                  # members' total volume

    def test_basket_shadow_grades_hit_on_synthetic_tick(self):
        import json
        import quant.engine as eng
        from quant.engine import shadow_process
        c = store.connect(self.db)
        cache = {}
        t0 = datetime(2026, 6, 20, tzinfo=timezone.utc)
        iso = lambda h: (t0 + timedelta(hours=h)).isoformat(timespec="seconds")
        for h, px in [(0, 100.0), (1, 101.0), (2, 105.0), (3, 108.0)]:   # index climbs
            store.insert_ticks(c, iso(h), [("__BASKET__", "ninja", px, 50000)], cache)
        store.predict_write(c, "p:b", "b", "BASKET", "__BASKET__",
                            {"p_hit": 0.5, "p_model": 0.5, "H_h": 72.0,
                             "entry": 101.0, "target": 106.0, "model": eng.MODEL_V})
        c.commit()
        shadow = {"orders": [{"pid": "p:b", "sig": "BASKET", "item": "__BASKET__",
                  "px": 101.0, "target": 106.0, "qty": 1, "H_h": 72.0, "ts": iso(0)}],
                  "pos": [], "watch": []}
        calib = calib_default(self.cfg["adv"])
        shadow_process(c, shadow, iso(3), self.cfg["adv"], calib)
        out = json.loads(c.execute("SELECT outcome FROM predictions WHERE id='p:b'").fetchone()[0])
        c.close()
        self.assertEqual(out["filled"], 1)               # 101 ≤ 101 entry → fills
        self.assertEqual(out["hit"], 1)                  # 108 ≥ 106 target

    def test_basket_synthetic_row_never_leaks_as_tradable(self):
        self.run_poll()
        self.io.step(1)
        snap = self.run_poll()
        c = store.connect(self.db)
        names = store.kv_json(c, "item_names") or []
        n_basket = c.execute("SELECT COUNT(*) FROM ticks WHERE item='__BASKET__'").fetchone()[0]
        c.close()
        self.assertGreater(n_basket, 0)                                    # tick IS written
        self.assertFalse([n for n in names if n.startswith("__")])          # never scannable
        self.assertFalse([r for r in snap["scan"] if r["item"].startswith("__")])  # never in scan
        self.assertNotIn("__BASKET__", [p["item"] for p in snap["port"]["positions"]])

    def test_unfilled_entry_expires_to_watch_then_grades_at_horizon(self):
        snap = self.play_script(dip_script())
        self.assertTrue([c for c in snap["cards"] if c["act"] == "DIP"])
        # price runs away instead of filling; past the fill window the order leaves
        # the book — but under Task 4 it is PRICE-WATCHED to H_h, not graded yet
        self.io.test_px = 99.0
        for _ in range(8):
            self.run_poll()
            self.io.step(1)
        c = store.connect(self.db)
        shadow = store.kv_json(c, "shadow")
        self.assertFalse([o for o in shadow["orders"]
                          if o["item"] == "Test Orb" and o["sig"] == "DIP"])  # left the book
        watched = [w for w in shadow.get("watch", [])
                   if w["item"] == "Test Orb" and w["sig"] == "DIP"]
        self.assertTrue(watched)                                             # now price-watched
        graded = store.predictions_graded(c, 30)
        self.assertFalse([g for g in graded if g["item"] == "Test Orb"
                          and g["sig"] == "DIP" and g["out"].get("filled") == 0])  # not graded yet
        c.close()
        # jump past the forecast horizon; the watch resolves into a graded fill-miss
        # that also carries the fill-independent price outcome (touch)
        self.io.step(max(w["H_h"] for w in watched) + 1)
        self.run_poll()
        c = store.connect(self.db)
        graded = store.predictions_graded(c, 30)
        dip_miss = [g for g in graded if g["item"] == "Test Orb" and g["sig"] == "DIP"
                    and g["out"].get("filled") == 0]
        self.assertTrue(dip_miss)                                            # graded at H_h
        self.assertIn("touch", dip_miss[0]["out"])                           # with the diagnostic
        shadow = store.kv_json(c, "shadow")
        self.assertFalse([w for w in shadow.get("watch", [])
                          if w["item"] == "Test Orb"])                       # watch cleared
        c.close()


if __name__ == "__main__":
    unittest.main()
