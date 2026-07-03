import contextlib
import io
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant import store
from quant.backtest import DEFAULT_GRID, load_ticks, run, sweep
from quant.config import load as load_cfg

FIX = Path(__file__).resolve().parent / "fixtures"


def _iso(base, hours):
    return (base + timedelta(hours=hours)).isoformat(timespec="seconds")


def _build_route_history(db_path, polls=40):
    """One item priced flat-ish by ninja (always > 10) with a divergent pair
    book: exalted=10, divine=12 (+20% -> a ROUTE candidate that clears fees).
    ninja never crosses 10, so the ROUTE entry can never fill."""
    c = store.connect(db_path)
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(polls):
        ts = _iso(base, i)
        nprice = 11.0 + 0.2 * math.sin(i)          # 10.8..11.2, never <= 10
        c.execute("INSERT INTO ticks VALUES(?,?,?,?,?)", (ts, "Test Orb", "ninja", nprice, 200))
        c.execute("INSERT INTO ticks VALUES(?,?,?,?,?)", (ts, "Test Orb", "pairex", 10.0, 20))
        c.execute("INSERT INTO ticks VALUES(?,?,?,?,?)", (ts, "Test Orb", "pairdiv", 12.0, 20))
    c.commit()
    c.close()


class TestLoadTicks(unittest.TestCase):
    def test_merges_archive_and_db_and_skips_synthetic(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            c = store.connect(db)
            c.execute("INSERT INTO ticks VALUES(?,?,?,?,?)",
                      ("2026-06-01T00:00:00+00:00", "Beta Orb", "ninja", 7.0, 90))
            c.commit()
            c.close()
            rows = load_ticks(db, FIX)                # FIX holds ticks-2026-05.csv
            items = {r[1] for r in rows}
            self.assertIn("Alpha Orb", items)         # from the archive CSV
            self.assertIn("Beta Orb", items)          # from the live DB
            self.assertNotIn("__BASKET__", items)     # synthetic row skipped
            tss = [r[0] for r in rows]
            self.assertEqual(tss, sorted(tss))        # returned sorted by ts


class TestRouteReplay(unittest.TestCase):
    def test_divergent_pairbook_makes_route_orders_that_never_fill(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            _build_route_history(db)
            rep = run(load_cfg(), db_path=db, archive_dir=os.path.join(d, "noarch"),
                      quiet=True)
            self.assertIsNotNone(rep)
            self.assertIn("ROUTE", rep["orders_by_sig"])       # ROUTE was replayed
            # every ROUTE order rested against a ninja price that never crossed
            # the exalted entry -> zero fills (the phantom-arb lesson, in a test)
            self.assertGreater(rep["orders_by_sig"]["ROUTE"]["placed"], 0)
            self.assertEqual(rep["orders_by_sig"]["ROUTE"]["filled"], 0)


class TestSweep(unittest.TestCase):
    def test_two_combo_sweep_returns_two_distinct_reports(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            _build_route_history(db)
            with contextlib.redirect_stdout(io.StringIO()):
                reports = sweep(load_cfg(), {"dip_z": [1.5, 2.2]}, db_path=db,
                                archive_dir=os.path.join(d, "noarch"))
            self.assertEqual(len(reports), 2)
            combos = [combo for combo, _ in reports]
            self.assertNotEqual(combos[0], combos[1])          # distinct combos
            self.assertTrue(all(rep is not None for _, rep in reports))

    def test_default_grid_shape(self):
        self.assertEqual(set(DEFAULT_GRID), {"dip_z", "idio_z", "dip_p_aim"})


if __name__ == "__main__":
    unittest.main()
