from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402


class PriceModelTest(unittest.TestCase):
    def test_long_run_stays_inside_band(self) -> None:
        """60000 个 tick 大致相当于 41 天游戏时间，价格不能漂出合理区间。"""
        random.seed(20261107)
        sim = engine.Engine()
        for _ in range(60_000):
            sim.tick += 1
            sim.move_prices()
            sim.maybe_news()

        for pair, price in sim.prices.items():
            anchor = engine.PAIR_ANCHOR[pair]
            self.assertGreater(price, 0.0, pair)
            self.assertLessEqual(
                abs(math.log(price / anchor)),
                engine.MAX_DEVIATION + 1e-9,
                pair,
            )

    def test_extreme_price_is_clamped(self) -> None:
        sim = engine.Engine()
        for pair in engine.PAIR_CFG:
            self.assertGreater(sim._clamp_price(pair, 1e-9), 1.0)
            self.assertLess(sim._clamp_price(pair, 1e12), 1000.0)
            self.assertEqual(sim._clamp_price(pair, -1.0), engine.PAIR_ANCHOR[pair])


if __name__ == "__main__":
    unittest.main()
