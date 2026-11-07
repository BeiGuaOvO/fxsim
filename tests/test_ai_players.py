from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402


class AIPlayerTest(unittest.TestCase):
    def setUp(self) -> None:
        random.seed(20261107)
        self.persist = patch.object(engine.store, "save_meta")
        self.persist.start()
        self.addCleanup(self.persist.stop)
        self.sim = engine.Engine()

    def test_add_ai_appears_on_leaderboard_and_profile(self) -> None:
        ai = self.sim.add_ai("trend")

        self.assertLess(ai.uid, 0)
        self.assertTrue(self.sim.is_ai(ai.uid))
        self.assertEqual(self.sim.ai_uid_by_name(ai.name), ai.uid)

        with patch.object(engine.store, "usernames", return_value={}):
            rows = self.sim.leaderboard()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_ai"])
        self.assertEqual(rows[0]["persona_label"], "趋势猎手")

        profile = self.sim.profile_view(ai.uid)
        self.assertTrue(profile["is_ai"])
        self.assertEqual(profile["name"], ai.name)
        self.assertEqual(profile["persona_label"], "趋势猎手")
        self.assertEqual(profile["principal"], round(engine.START_BALANCE))

    def test_ai_trades_do_not_touch_database(self) -> None:
        ai = self.sim.add_ai("degen")
        leverage = engine.LEVERAGES[0]

        with (
            patch.object(engine.store, "insert_position") as insert_position,
            patch.object(engine.store, "insert_trade") as insert_trade,
            patch.object(engine.store, "delete_position") as delete_position,
            patch.object(engine.store, "save_account") as save_account,
        ):
            pos = self.sim.open_position(ai.uid, "USDJPY", 1, 10_000, leverage)
            pnl = self.sim.close_position(pos.id, uid=ai.uid, reason="ai")

        insert_position.assert_not_called()
        insert_trade.assert_not_called()
        delete_position.assert_not_called()
        save_account.assert_not_called()
        self.assertEqual(len(ai.trades), 1)
        self.assertEqual(ai.trades[0]["reason"], "ai")
        self.assertEqual(ai.trades[0]["pnl"], round(pnl))

    def test_ai_trades_automatically(self) -> None:
        ai = self.sim.add_ai("news")

        for _ in range(140):
            self.sim.tick += 1
            self.sim.move_prices()
            self.sim.maybe_news()
            self.sim.run_ai()
            self.sim.check_risk()

        has_position = any(
            p.user_id == ai.uid for p in self.sim.positions.values()
        )
        self.assertTrue(ai.trades or has_position, "AI should trade after the first news")

    def test_ai_snapshot_keeps_curve_without_database(self) -> None:
        ai = self.sim.add_ai("trend")

        with (
            patch.object(engine.store, "snapshot_equity") as snapshot_equity,
            patch.object(engine.store, "save_account") as save_account,
            patch.object(engine.store, "save_meta"),
        ):
            self.sim.snapshot()

        snapshot_equity.assert_not_called()
        save_account.assert_not_called()
        self.assertEqual(len(ai.curve), 1)
        self.assertEqual(ai.curve[0]["equity"], round(engine.START_BALANCE))

    def test_clear_ai(self) -> None:
        self.sim.add_ai("trend")
        self.sim.add_ai("news")

        removed = self.sim.clear_ai()

        self.assertEqual(removed, 2)
        self.assertEqual(self.sim.ai, {})
        self.assertFalse(any(uid < 0 for uid in self.sim.accounts))


if __name__ == "__main__":
    unittest.main()
