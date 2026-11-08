from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import engine, replay  # noqa: E402


def make_data(n: int = 60) -> replay.ReplayData:
    base = 1_700_000_000
    raw = {}
    for pi, pair in enumerate(replay.PAIRS):
        points = []
        for i in range(n):
            if pair == "EURJPY" and i == 3:
                continue
            points.append((base + i * 300, 100.0 + pi * 10 + i * 0.1))
        raw[pair] = points
    return replay.build_replay_data(raw)


class ReplayDataTest(unittest.TestCase):
    def test_aligns_pairs_and_forward_fills_missing_bar(self) -> None:
        data = make_data(10)
        self.assertEqual(len(data.times), 10)
        self.assertEqual(data.times[1] - data.times[0], 300)
        # EURJPY 第 3 根缺失，应该沿用第 2 根价格
        self.assertEqual(data.prices["EURJPY"][3], data.prices["EURJPY"][2])
        self.assertEqual(data.prices["USDJPY"][0], 100.0)

    def test_rejects_incomplete_data(self) -> None:
        with self.assertRaises(replay.ReplayError):
            replay.build_replay_data({})

    def test_parse_yahoo_all_keeps_all_points(self) -> None:
        payload = {
            "chart": {"result": [{
                "timestamp": [1000, 1060, 1120],
                "indicators": {"quote": [{"close": [1.0, None, 3.0]}]},
            }]}
        }
        self.assertEqual(
            replay.parse_yahoo_all("USDJPY", payload),
            [(1000.0, 1.0), (1120.0, 3.0)],
        )


class ReplayEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = replay.ReplayEngine()
        self.data = make_data(60)
        self.engine.data = self.data

    def test_start_clamps_to_data_range(self) -> None:
        session = self.engine.start(1, self.data.times[0] - 999, 10)
        self.assertGreaterEqual(session.start_idx, 0)
        self.assertGreaterEqual(session.cursor, session.start_idx)
        self.assertEqual(session.account["start_balance"], replay.START_BALANCE)

    def test_advance_and_state(self) -> None:
        session = self.engine.start(1, self.data.times[5], 10)
        session.last_wall = time.time() - 1.0
        self.engine.advance(session)
        self.assertGreater(session.cursor, 5)

        state = self.engine.state_view(session)
        self.assertTrue(state["active"])
        self.assertGreater(state["progress"], 0)
        self.assertIn("USDJPY", state["prices"])

    def test_open_and_close_use_replay_timestamps(self) -> None:
        session = self.engine.start(1, self.data.times[5], 10)
        pos = self.engine.open_position(session, "USDJPY", 1, 10_000,
                                        engine.LEVERAGES[0])
        self.assertEqual(pos.opened_at, self.data.times[5])

        session.cursor = 15
        pnl = self.engine.close_position(session, pos.id)
        self.assertGreater(pnl, 0)
        self.assertEqual(session.trades[0]["closed_at"], self.data.times[15])
        self.assertEqual(session.trades[0]["opened_at"], self.data.times[5])
        self.assertEqual(session.positions, {})

    def test_history_is_limited_to_cursor(self) -> None:
        session = self.engine.start(1, self.data.times[0], 10)
        session.cursor = 20
        history = self.engine.history_view(session, "USDJPY", 10)
        self.assertEqual(len(history["points"]), 10)
        self.assertEqual(history["points"][-1][0], self.data.times[20])
        self.assertEqual(history["points"][-1][1], round(self.data.prices["USDJPY"][20], 3))

    def test_pause_resume_and_end(self) -> None:
        session = self.engine.start(1, self.data.times[-2], 10)
        self.engine.control(session, playing=False)
        self.assertFalse(session.playing)
        self.engine.control(session, playing=True)
        self.assertTrue(session.playing)

        session.cursor = len(self.data.times) - 2
        session.last_wall = time.time() - 1.0
        self.engine.advance(session)
        self.assertTrue(session.ended)
        self.assertFalse(session.playing)


if __name__ == "__main__":
    unittest.main()
