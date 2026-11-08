from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import engine, real  # noqa: E402


class QuoteParserTest(unittest.TestCase):
    def test_swissquote_uses_median_mid_and_millisecond_timestamp(self) -> None:
        payload = [
            {
                "ts": 1794002400083,
                "spreadProfilePrices": [
                    {"bid": 158.50, "ask": 158.54},
                    {"bid": 158.52, "ask": 158.56},
                ],
            },
            {
                "ts": 1794002400087,
                "spreadProfilePrices": [{"bid": 158.60, "ask": 158.64}],
            },
        ]
        quote = real.parse_swissquote("USDJPY", payload)
        self.assertIsNotNone(quote)
        self.assertAlmostEqual(quote["price"], 158.54, places=6)
        self.assertAlmostEqual(quote["ts"], 1794002400.087, places=3)
        self.assertEqual(quote["source"], "Swissquote")

    def test_swissquote_rejects_empty_or_invalid_payload(self) -> None:
        self.assertIsNone(real.parse_swissquote("USDJPY", []))
        self.assertIsNone(real.parse_swissquote("USDJPY", {"bad": True}))
        self.assertIsNone(real.parse_swissquote("USDJPY", [{"spreadProfilePrices": []}]))

    def test_frankfurter_builds_all_jpy_crosses(self) -> None:
        payload = {
            "date": "2026-11-06",
            "rates": {"JPY": 158.33, "EUR": 0.89566, "GBP": 0.74711,
                      "AUD": 1.4554, "CHF": 0.8189},
        }
        quotes = real.parse_frankfurter(payload)
        self.assertEqual(set(quotes), set(real.PAIRS))
        self.assertAlmostEqual(quotes["USDJPY"]["price"], 158.33)
        self.assertAlmostEqual(quotes["EURJPY"]["price"], 158.33 / 0.89566)
        self.assertAlmostEqual(quotes["GBPJPY"]["price"], 158.33 / 0.74711)
        self.assertEqual(quotes["USDJPY"]["source"], "Frankfurter 日线")

    def test_yahoo_history_filters_nulls(self) -> None:
        payload = {
            "chart": {
                "result": [{
                    "timestamp": [1000, 1060, 1120],
                    "indicators": {"quote": [{"close": [158.1, None, 158.3]}]},
                }],
                "error": None,
            }
        }
        points = real.parse_yahoo_history("USDJPY", payload)
        self.assertEqual(points, [(1000.0, 158.1), (1120.0, 158.3)])


class RealEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = real.RealEngine()
        self.engine.accounts[1] = {
            "user_id": 1,
            "balance": 1_000_000.0,
            "start_balance": 1_000_000.0,
            "best_equity": 1_000_000.0,
            "status": "active",
            "updated_at": 0.0,
        }
        self.engine.prices["USDJPY"] = 158.0

    def test_apply_quotes_updates_prices_and_ticks(self) -> None:
        ts = time.time()
        self.engine.apply_quotes({
            "USDJPY": {"pair": "USDJPY", "price": 158.2, "ts": ts, "source": "Swissquote"}
        })
        self.assertAlmostEqual(self.engine.prices["USDJPY"], 158.2)
        self.assertEqual(len(self.engine.ticks["USDJPY"]), 1)
        self.assertFalse(self.engine.market_view()["stale"])

        self.engine.last_update = 0
        self.assertTrue(self.engine.market_view()["stale"])

    def test_history_merges_before_live_ticks(self) -> None:
        self.engine.ticks["USDJPY"].append((1000.0, 158.0))
        self.engine.apply_history({"USDJPY": [(900.0, 157.0), (950.0, 157.5)]})
        points = list(self.engine.ticks["USDJPY"])
        self.assertEqual([p[0] for p in points], [900.0, 950.0, 1000.0])
        self.assertEqual(self.engine.history_view("USDJPY")["points"][-1][1], 158.0)

    def test_open_and_close_position_without_real_database(self) -> None:
        leverage = engine.LEVERAGES[0]
        with (
            patch.object(real.real_store, "insert_position", return_value=7) as insert,
            patch.object(real.real_store, "insert_trade") as insert_trade,
            patch.object(real.real_store, "delete_position") as delete,
            patch.object(real.real_store, "save_account") as save,
        ):
            pos = self.engine.open_position(1, "USDJPY", 1, 10_000, leverage)
            self.assertEqual(pos.id, 7)
            insert.assert_called_once()

            self.engine.prices["USDJPY"] = 159.0
            pnl = self.engine.close_position(7, uid=1)
            self.assertGreater(pnl, 0)
            self.assertEqual(self.engine.positions, {})
            insert_trade.assert_called_once()
            delete.assert_called_once()
            save.assert_called_once()
            self.assertGreater(self.engine.accounts[1]["balance"], 1_000_000.0)

    def test_account_equity_uses_entry_when_price_is_missing(self) -> None:
        with patch.object(real.real_store, "insert_position", return_value=3):
            self.engine.open_position(1, "USDJPY", 1, 10_000, engine.LEVERAGES[0])
        self.engine.prices["USDJPY"] = None
        equity, used = self.engine.account_equity(1)
        self.assertEqual(equity, 1_000_000.0)
        self.assertGreater(used, 0)


if __name__ == "__main__":
    unittest.main()
