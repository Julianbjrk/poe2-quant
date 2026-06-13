import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant import store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()


class TestLedger(StoreCase):
    def test_fold_with_void(self):
        c = store.connect(self.path)
        b = store.append(c, "fill", {"ledger": "paper", "item": "X", "side": "buy",
                                     "qty": 3, "px": 10, "target_px": 12})
        store.append(c, "fill", {"ledger": "paper", "item": "X", "side": "sell", "qty": 1, "px": 12})
        pos = store.positions(c, "paper")
        self.assertEqual(pos["X"]["qty"], 2)
        self.assertEqual(pos["X"]["target_px"], 12)
        store.append(c, "fill_void", {"void_id": b})
        self.assertNotIn("X", store.positions(c, "paper"))  # over-sell clamps, qty 0

    def test_ledgers_never_mix(self):
        c = store.connect(self.path)
        store.append(c, "fill", {"ledger": "paper", "item": "X", "side": "buy", "qty": 1, "px": 10})
        store.append(c, "fill", {"ledger": "real", "item": "Y", "side": "buy", "qty": 1, "px": 10})
        self.assertEqual(list(store.positions(c, "paper")), ["X"])
        self.assertEqual(list(store.positions(c, "real")), ["Y"])

    def test_random_event_storm_keeps_invariants(self):
        random.seed(11)
        c = store.connect(self.path)
        ids = []
        for _ in range(300):
            op = random.random()
            if op < 0.5:
                ids.append(store.append(c, "fill", {
                    "ledger": "paper", "item": random.choice("ABC"),
                    "side": random.choice(["buy", "sell"]),
                    "qty": random.randint(1, 5), "px": random.uniform(1, 100)}))
            elif ids:
                store.append(c, "fill_void", {"void_id": random.choice(ids)})
            pos = store.positions(c, "paper")
            for item, st in pos.items():
                self.assertGreaterEqual(st["qty"], 0)
                self.assertEqual(st["cost_ex"], st["cost_ex"])  # not NaN
                self.assertGreaterEqual(st["avg"], 0)

    def test_edit_via_void_and_reappend_refolds(self):
        # the fill-edit path: a wrong price is corrected by voiding + re-adding;
        # positions must reflect the new number, not the old
        c = store.connect(self.path)
        bad = store.append(c, "fill", {"ledger": "paper", "item": "X", "side": "buy",
                                       "qty": 2, "px": 200, "card_id": "k1",
                                       "target_px": 240, "note": "card"})
        self.assertAlmostEqual(store.positions(c, "paper")["X"]["avg"], 200)
        orig = store.event_by_id(c, bad)
        store.append(c, "fill_void", {"void_id": bad})
        store.append(c, "fill", {"ledger": "paper", "item": "X", "side": "buy",
                                 "qty": 2, "px": 100, "card_id": orig["card_id"],
                                 "target_px": orig["target_px"], "note": "edit"})
        pos = store.positions(c, "paper")["X"]
        self.assertAlmostEqual(pos["avg"], 100)        # corrected price
        self.assertEqual(pos["target_px"], 240)        # card linkage preserved

    def test_pending_orders(self):
        c = store.connect(self.path)
        o1 = store.append(c, "order", {"ledger": "paper", "item": "X", "side": "buy",
                                       "qty": 1, "px": 10})
        o2 = store.append(c, "order", {"ledger": "paper", "item": "Y", "side": "buy",
                                       "qty": 1, "px": 10})
        store.append(c, "order_cancel", {"void_id": o1})
        store.append(c, "fill", {"ledger": "paper", "item": "Y", "side": "buy",
                                 "qty": 1, "px": 10, "order_id": o2})
        self.assertEqual(store.pending_orders(c, "paper"), [])


class TestTicks(StoreCase):
    def test_dedupe_and_bars(self):
        c = store.connect(self.path)
        cache = {}
        n = store.insert_ticks(c, "2026-06-12T10:00:00+00:00",
                               [("X", "ninja", 10.0, 100)], cache)
        n += store.insert_ticks(c, "2026-06-12T10:05:00+00:00",
                                [("X", "ninja", 10.0, 100)], cache)  # unchanged → skipped
        n += store.insert_ticks(c, "2026-06-12T10:10:00+00:00",
                                [("X", "ninja", 11.0, 100)], cache)
        self.assertEqual(n, 2)
        bars = store.hourly_closes(c, "X")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0][1], 11.0)
        series = store.ticks_since(c, "2026-06-12T10:01:00+00:00", ["X"])
        self.assertEqual(len(series["X"]), 1)


class TestArchive(StoreCase):
    def test_prune_archives_old_ticks_then_deletes_keeps_recent(self):
        c = store.connect(self.path)
        cache = {}
        new_ts = store.now_iso()
        store.insert_ticks(c, "2020-01-15T10:00:00+00:00", [("X", "ninja", 10.0, 100)], cache)
        store.insert_ticks(c, new_ts, [("X", "ninja", 11.0, 100)], cache)
        c.commit()
        arch = Path(self.tmp.name) / "data_archive"
        store.prune(c, tick_days=14, snap_days=60, archive_dir=str(arch))
        c.commit()
        # the old tick is gone from the DB but preserved in a monthly CSV; recent stays
        self.assertEqual([r[0] for r in c.execute("SELECT ts FROM ticks")], [new_ts])
        f = arch / "ticks-2020-01.csv"
        self.assertTrue(f.exists())
        lines = f.read_text().strip().splitlines()
        self.assertEqual(lines[0], "ts,item,source,price_ex,vol_div")
        self.assertIn("2020-01-15T10:00:00+00:00,X,ninja,10.0,100", lines[1])

    def test_archive_appends_across_prunes(self):
        c = store.connect(self.path)
        cache = {}
        store.insert_ticks(c, "2020-01-15T10:00:00+00:00", [("X", "ninja", 10.0, 100)], cache)
        c.commit()
        arch = str(Path(self.tmp.name) / "data_archive")
        store.prune(c, 14, 60, archive_dir=arch)
        store.insert_ticks(c, "2020-01-20T10:00:00+00:00", [("X", "ninja", 12.0, 100)], cache)
        c.commit()
        store.prune(c, 14, 60, archive_dir=arch)
        lines = (Path(arch) / "ticks-2020-01.csv").read_text().strip().splitlines()
        self.assertEqual(len(lines), 3)  # header + two appended ticks, no dup header

    def test_archive_failure_keeps_ticks(self):
        c = store.connect(self.path)
        cache = {}
        store.insert_ticks(c, "2020-01-15T10:00:00+00:00", [("X", "ninja", 10.0, 100)], cache)
        c.commit()
        blocker = Path(self.tmp.name) / "afile"
        blocker.write_text("not a dir")
        store.prune(c, 14, 60, archive_dir=str(blocker / "sub"))  # mkdir under a file -> fails
        c.commit()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ticks").fetchone()[0], 1)  # not lost

    def test_export_bundle(self):
        c = store.connect(self.path)
        cache = {}
        store.insert_ticks(c, store.now_iso(), [("X", "ninja", 10.0, 100)], cache)
        store.predict_write(c, "p1", "c1", "DIP", "X", {"p_hit": 0.6, "feat": {"z": -2}})
        store.predict_grade(c, "p1", {"filled": 1, "hit": 1, "realized_pct": 4.0})
        c.commit()
        out = Path(self.tmp.name) / "exp"
        res = store.export_all(c, str(out))
        self.assertEqual(res["predictions"], 1)
        for name in ("predictions.jsonl", "bars.csv", "ticks_live.csv"):
            self.assertTrue((out / name).exists(), name)
        rec = json.loads((out / "predictions.jsonl").read_text().strip())
        self.assertEqual(rec["outcome"]["hit"], 1)
        self.assertEqual(rec["forecast"]["p_hit"], 0.6)


class TestMigration(StoreCase):
    def test_v04_db_is_imported(self):
        c = sqlite3.connect(self.path)
        c.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY, ts TEXT, play_id TEXT,"
                  " side TEXT, qty REAL, price_ex REAL, note TEXT, paper INTEGER DEFAULT 0)")
        c.execute("INSERT INTO fills(ts,play_id,side,qty,price_ex,note,paper) "
                  "VALUES('2026-06-01T00:00:00+00:00','c:Old Orb','buy',2,50,'',1)")
        c.execute("CREATE TABLE ticks(ts TEXT, item TEXT, typ TEXT, price_ex REAL, vol_div REAL)")
        c.execute("INSERT INTO ticks VALUES('2026-06-01T00:00:00+00:00','Old Orb','Currency',50,900)")
        c.execute("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO kv VALUES('holdings', ?)",
                  (json.dumps({"div": 3, "ex": 100, "chaos": 0, "ts": "2026-06-01T00:00:00+00:00"}),))
        c.commit()
        c.close()
        c = store.connect(self.path)
        pos = store.positions(c, "paper")
        self.assertEqual(pos["Old Orb"]["qty"], 2)
        self.assertIsNotNone(store.holdings(c))
        src = c.execute("SELECT source FROM ticks").fetchone()[0]
        self.assertEqual(src, "ninja")


if __name__ == "__main__":
    unittest.main()
