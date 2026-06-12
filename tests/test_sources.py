import json
import unittest
from pathlib import Path

from quant.sources import SourceError, parse_ninja, parse_pairs

FIX = Path(__file__).parent / "fixtures"


class TestNinja(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((FIX / "ninja_currency.json").read_text())

    def test_prices_volumes_rate(self):
        d = parse_ninja(self.raw)
        self.assertAlmostEqual(d["ex_per_div"], 400.0)
        self.assertAlmostEqual(d["price_ex"]["Test Orb"], 100.0)
        self.assertAlmostEqual(d["vol_div"]["Test Orb"], 2000.0)
        self.assertEqual(d["trend"]["Test Orb"], -2.0)

    def test_contract_violation_raises(self):
        with self.assertRaises(SourceError):
            parse_ninja({"unexpected": True})


class TestPairs(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((FIX / "scout_pairs.json").read_text())

    def test_routes_and_sanity(self):
        routes, note = parse_pairs(self.raw, ex_per_div=400.0)
        self.assertIsNone(note)
        self.assertAlmostEqual(routes["Test Orb"]["exalted"]["px_ex"], 100.0)
        self.assertAlmostEqual(routes["Test Orb"]["divine"]["px_ex"], 112.0)

    def test_bad_divine_check_quarantines_everything(self):
        routes, note = parse_pairs(self.raw, ex_per_div=200.0)
        self.assertEqual(routes, {})
        self.assertIn("sanity check", note)


if __name__ == "__main__":
    unittest.main()
