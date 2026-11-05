"""Tiingo prices and technicals, with no network access."""

import json
import os
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import patch

import market_data as md

TODAY = date(2026, 11, 5)


def rows(closes):
    return [{"date": f"2025-01-{i % 28 + 1:02d}T00:00:00.000Z", "close": c * 2, "adjClose": c}
            for i, c in enumerate(closes)]


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class IndicatorTests(unittest.TestCase):
    def test_sma_and_rsi_edges(self):
        self.assertIsNone(md.sma([1.0] * 49, 50))
        self.assertEqual(md.sma([1.0] * 50 + [3.0] * 50, 50), 3.0)
        self.assertIsNone(md.rsi([1.0] * 14))
        self.assertEqual(md.rsi([float(i) for i in range(30)]), 100.0)
        self.assertEqual(md.rsi([float(30 - i) for i in range(30)]), 0.0)
        alternating = [100.0 + (i % 2) for i in range(60)]
        self.assertAlmostEqual(md.rsi(alternating), 50.0, delta=2.0)

    def test_technicals_from_a_series(self):
        closes = [100.0] * 200 + [150.0] * 50 + [120.0]
        series = [(f"d{i}", c) for i, c in enumerate(closes)]
        t = md.technicals("AAA", series)
        self.assertEqual(t.last_date, "d250")
        self.assertAlmostEqual(t.from_52w_high, -20.0)
        self.assertAlmostEqual(t.vs_sma50, (120 / ((150 * 49 + 120) / 50) - 1) * 100)
        self.assertAlmostEqual(t.ret_1m, -20.0)
        self.assertIsNone(md.technicals("AAA", []))
        short = md.technicals("AAA", series[:30])
        self.assertIsNone(short.vs_sma50)
        self.assertIsNone(short.ret_3m)


class LoadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.prices = Path(directory.name) / "prices"
        env = patch.dict(os.environ, {"TIINGO_API_KEY": "secret-key"})
        env.start()
        self.addCleanup(env.stop)

    def test_fetch_uses_header_auth_adjusted_closes_and_caches(self):
        seen = []

        def fake_urlopen(request, timeout):
            seen.append(request)
            return FakeResponse(rows([10.0, 11.0, 12.0]))

        with patch.object(md.urllib.request, "urlopen", side_effect=fake_urlopen):
            out, problems = md.load_technicals(["BRK.B", "CASH", "AAA 100C"], TODAY,
                                               self.prices, fetch=True)
            self.assertEqual(problems, [])
            self.assertEqual(sorted(out), ["BRK.B", "QQQ", "SMH", "SPY"])
            self.assertEqual(out["SPY"].last_close, 12.0)
            urls = [r.full_url for r in seen]
            self.assertTrue(any("/daily/brk-b/prices?startDate=2025-10-01" in u for u in urls))
            self.assertTrue(all("secret-key" not in u for u in urls))
            self.assertEqual(seen[0].get_header("Authorization"), "Token secret-key")
            again, _ = md.load_technicals(["BRK.B"], TODAY, self.prices, fetch=True)
        self.assertEqual(len(seen), 4)
        self.assertEqual(sorted(again), ["BRK.B", "QQQ", "SMH", "SPY"])

    def test_http_error_is_reported_not_raised(self):
        error = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with patch.object(md.urllib.request, "urlopen", side_effect=error):
            out, problems = md.load_technicals(["AAA"], TODAY, self.prices, fetch=True)
        self.assertEqual(out, {})
        self.assertIn("AAA: 429", problems)

    def test_missing_key_and_offline_use_the_cache(self):
        self.prices.mkdir()
        cached = {"fetched": "2026-11-01", "closes": [["2026-10-31", 5.0]]}
        (self.prices / "SPY.json").write_text(json.dumps(cached))
        with patch.dict(os.environ, {"TIINGO_API_KEY": ""}), \
                patch.object(md.urllib.request, "urlopen") as urlopen:
            out, problems = md.load_technicals([], TODAY, self.prices, fetch=True)
            urlopen.assert_not_called()
        self.assertEqual(list(out), ["SPY"])
        self.assertEqual(problems, ["TIINGO_API_KEY is not set in .env"])
        out, problems = md.load_technicals([], TODAY, Path(self.prices.parent / "none"),
                                           fetch=False)
        self.assertEqual((out, problems), ({}, ["no cached prices (offline build)"]))


if __name__ == "__main__":
    unittest.main()
