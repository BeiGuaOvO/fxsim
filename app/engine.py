from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import deque

from . import store

log = logging.getLogger("fx.engine")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_floats(name: str, default: str) -> tuple[float, ...]:
    out = []
    for part in os.getenv(name, default).replace(" ", "").split(","):
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return tuple(sorted(set(out))) or (10.0, 50.0, 100.0)


TICK_SECONDS = _env_float("FX_TICK_SECONDS", 1.0)
START_BALANCE = store.START_BALANCE
GOAL = _env_float("FX_GOAL", 20_000_000.0)
CONTRACT = 100_000.0
CANDLE_TICKS = max(1, int(_env_float("FX_CANDLE_TICKS", 5.0)))

# 玩家可选的杠杆倍数
LEVERAGES = _env_floats("FX_LEVERAGES", "10,50,100,200,500")
MIN_AMOUNT = _env_float("FX_MIN_AMOUNT", 1_000.0)
MAX_AMOUNT = _env_float("FX_MAX_AMOUNT", 10_000_000.0)
MAX_POSITIONS = 10
MAX_AI = max(0, min(20, int(_env_float("FX_MAX_AI", 8.0))))
# 风控三档
STOPOUT = _env_float("FX_STOPOUT", 50.0)             # 爆仓线：维持率跌破即全部强平
WARN = _env_float("FX_WARN", 130.0)                  # 预警线：低于此值提示补仓
TOPUP_TARGET = _env_float("FX_TOPUP_TARGET", 160.0)  # 补仓后希望回到的维持率
MAX_DEPOSIT = _env_float("FX_MAX_DEPOSIT", 1_000_000.0)  # 累计可追加上限
TOPUP_STEP = 10_000.0                                # 补仓金额取整单位
# 消息驱动行情
NEWS_ON = os.getenv("FX_NEWS", "1") != "0"
NEWS_MIN = _env_float("FX_NEWS_MIN", 90.0)
NEWS_MAX = _env_float("FX_NEWS_MAX", 240.0)
NEWS_GAP = _env_float("FX_NEWS_GAP", 0.35)   # 消息瞬间走完的比例
NEWS_SIZE = {
    "small": (0.002, 0.005),
    "mid":   (0.006, 0.012),
    "large": (0.015, 0.030),
    "xxl":   (0.040, 0.080),
}
# 抽中权重：越大越常见
NEWS_WEIGHT = {"small": 8, "mid": 10, "large": 5, "xxl": 3}
NEWS_LIB = [
    # ---------- 美国 ----------
    {"tag": "美国", "size": "mid", "title": "美国核心通胀高于预期",
     "body": "通胀比预期顽固，美联储可能推迟降息，美元通常走强。",
     "pairs": {"USDJPY": 1.0, "EURJPY": 0.2, "GBPJPY": 0.2}},
    {"tag": "美国", "size": "mid", "title": "美国核心通胀低于预期",
     "body": "通胀降温，市场更相信会降息，美元通常走弱。",
     "pairs": {"USDJPY": -1.0, "EURJPY": -0.2, "GBPJPY": -0.2}},
    {"tag": "美国", "size": "large", "title": "美国非农就业远超预期",
     "body": "就业强、经济热，美联储更难降息，美元走强。",
     "pairs": {"USDJPY": 1.0, "AUDJPY": 0.3}},
    {"tag": "美国", "size": "large", "title": "美国非农就业意外疲软",
     "body": "就业转弱，市场押注降息，美元走弱。",
     "pairs": {"USDJPY": -1.0, "AUDJPY": -0.3}},
    {"tag": "美联储", "size": "mid", "title": "美联储官员暗示推迟降息",
     "body": "利率会更高更久，美元有吸引力，同时打压风险资产。",
     "pairs": {"USDJPY": 0.9, "AUDJPY": -0.4}},
    {"tag": "美联储", "size": "mid", "title": "美联储释放降息信号",
     "body": "降息预期升温，美元走弱，资金更愿意冒险。",
     "pairs": {"USDJPY": -0.9, "AUDJPY": 0.4}},
    {"tag": "美国", "size": "large", "title": "美国评级被下调，避险情绪升温",
     "body": "资金逃离美元资产，买入日元和瑞郎避险。",
     "pairs": {"USDJPY": -1.0, "CHFJPY": 0.5, "AUDJPY": -0.6}},
    # ---------- 日本 ----------
    {"tag": "日本", "size": "large", "title": "日本央行入市干预日元",
     "body": "央行直接买日元，日元短线快速升值。",
     "pairs": {"USDJPY": -1.2, "EURJPY": -0.6, "GBPJPY": -0.6, "AUDJPY": -0.4}},
    {"tag": "日本", "size": "mid", "title": "日本央行暗示可能加息",
     "body": "日元利率上升，持有日元的吸引力增加，日元走强。",
     "pairs": {"USDJPY": -1.0, "EURJPY": -0.4}},
    {"tag": "日本", "size": "small", "title": "日本核心通胀超预期",
     "body": "物价涨得快，市场猜测日本会收紧政策，日元偏强。",
     "pairs": {"USDJPY": -0.8}},
    {"tag": "传闻", "size": "small", "title": "市场传闻日本央行即将干预",
     "body": "还没真动手，但大家先买日元，价格先跌一波。",
     "pairs": {"USDJPY": -0.7}},
    {"tag": "日本", "size": "small", "title": "原油价格快速上涨",
     "body": "日本几乎全靠进口能源，成本上升让日元偏弱。",
     "pairs": {"USDJPY": 0.6}},
    # ---------- 欧洲 / 英国 ----------
    {"tag": "欧洲", "size": "mid", "title": "欧洲央行意外加息",
     "body": "欧元利率上升，欧元走强。",
     "pairs": {"EURJPY": 1.1}},
    {"tag": "欧洲", "size": "mid", "title": "欧洲央行暗示降息",
     "body": "欧元利率要降，欧元走弱。",
     "pairs": {"EURJPY": -1.1}},
    {"tag": "英国", "size": "mid", "title": "英国通胀高于预期",
     "body": "市场预期英国央行继续加息，英镑走强。",
     "pairs": {"GBPJPY": 1.1}},
    {"tag": "英国", "size": "mid", "title": "英国经济数据明显走弱",
     "body": "经济不好，市场押注降息，英镑走弱。",
     "pairs": {"GBPJPY": -1.0}},
    # ---------- 商品货币 / 避险 ----------
    {"tag": "澳洲", "size": "mid", "title": "澳洲就业数据强劲",
     "body": "经济好、利率可能更高，澳元走强。",
     "pairs": {"AUDJPY": 1.0}},
    {"tag": "中国", "size": "mid", "title": "中国宣布大规模刺激政策",
     "body": "大宗商品需求预期上升，澳元等商品货币受益。",
     "pairs": {"AUDJPY": 1.0}},
    {"tag": "避险", "size": "large", "title": "全球股市大跌，资金寻求避险",
     "body": "风险资产被抛售，资金买入日元和瑞郎。",
     "pairs": {"CHFJPY": 1.0, "USDJPY": -0.5, "AUDJPY": -0.9}},
    {"tag": "避险", "size": "mid", "title": "市场情绪转暖，风险偏好回升",
     "body": "资金从避险货币流向高息货币。",
     "pairs": {"CHFJPY": -0.9, "AUDJPY": 0.8}},
    {"tag": "瑞士", "size": "large", "title": "瑞士央行意外降息",
     "body": "瑞郎利率下降，吸引力减弱，瑞郎走弱。",
     "pairs": {"CHFJPY": -1.2}},
    # ---------- 事故 ----------
    {"tag": "事故", "size": "mid", "title": "某机构误下大单，单一货币对瞬间跳水",
     "body": "乌龙指引发连锁止损，价格短时间被砸低。",
     "random_pair": True, "pairs": {}},
    # ---------- 黑天鹅 ----------
    {"tag": "黑天鹅", "size": "xxl", "title": "瑞士央行突然取消汇率下限",
     "body": "历史级事件：瑞郎瞬间暴涨，持有瑞郎空头的人直接被埋。",
     "pairs": {"CHFJPY": 1.0}},
    {"tag": "黑天鹅", "size": "xxl", "title": "中东局势突变，全球避险",
     "body": "恐慌情绪蔓延，日元和瑞郎被抢购，商品货币被抛售。",
     "pairs": {"CHFJPY": 1.2, "USDJPY": -0.8, "AUDJPY": -1.5}},
]
def pick_news() -> dict:
    total = sum(NEWS_WEIGHT[t["size"]] for t in NEWS_LIB)
    roll = random.uniform(0.0, total)
    acc = 0.0
    for tpl in NEWS_LIB:
        acc += NEWS_WEIGHT[tpl["size"]]
        if roll <= acc:
            return tpl
    return NEWS_LIB[-1]

PAIR_CFG = {
    "USDJPY": {"mid": 152.300, "vol": 0.0060},
    "EURJPY": {"mid": 165.200, "vol": 0.0065},
    "GBPJPY": {"mid": 194.500, "vol": 0.0080},
    "AUDJPY": {"mid": 99.100, "vol": 0.0075},
    "CHFJPY": {"mid": 172.400, "vol": 0.0055},
}

# ---------------- 价格锚定 ----------------
# 没有锚定的对数随机游走跑久了必然漂到 0 或无穷大，这里让价格围绕 mid
# 做均值回复（Ornstein-Uhlenbeck），并用一条软硬结合的边界兜底。
PRICE_HALFLIFE = max(120.0, _env_float("FX_PRICE_HALFLIFE", 1800.0))  # 回归半衰期（tick）
REVERSION = math.log(2.0) / PRICE_HALFLIFE
MAX_DEVIATION = min(0.90, max(0.05, _env_float("FX_PRICE_BAND", 0.35)))  # 相对 mid 的边界
PAIR_ANCHOR = {p: cfg["mid"] for p, cfg in PAIR_CFG.items()}
ANCHOR_LOG = {p: math.log(v) for p, v in PAIR_ANCHOR.items()}
PRICE_FLOOR_LOG = {p: ANCHOR_LOG[p] - MAX_DEVIATION for p in PAIR_CFG}
PRICE_CEIL_LOG = {p: ANCHOR_LOG[p] + MAX_DEVIATION for p in PAIR_CFG}


def _news_expected_bias() -> dict[str, float]:
    """新闻库里多空消息并不对称，算出每个货币对每条新闻的期望对数涨跌。"""
    total = sum(NEWS_WEIGHT[t["size"]] for t in NEWS_LIB) or 1
    bias = {p: 0.0 for p in PAIR_CFG}
    for tpl in NEWS_LIB:
        weight = NEWS_WEIGHT[tpl["size"]]
        lo, hi = NEWS_SIZE[tpl["size"]]
        size = (lo + hi) / 2.0
        if tpl.get("random_pair"):
            # 乌龙指：随机挑一个货币对向下砸，每个货币对的期望冲击相同
            for pair in bias:
                bias[pair] += weight * size * -1.0 / len(bias)
        else:
            for pair, k in tpl["pairs"].items():
                if pair in bias:
                    bias[pair] += weight * size * k
    return {p: v / total for p, v in bias.items()}


def _news_rate() -> float:
    """平均每个 tick 触发一条新闻的概率。"""
    if not NEWS_ON:
        return 0.0
    lo = max(10.0, NEWS_MIN)
    hi = max(NEWS_MIN + 10.0, NEWS_MAX)
    return 1.0 / ((lo + hi) / 2.0)


NEWS_BIAS = _news_expected_bias()
NEWS_BIAS_PER_TICK = {p: NEWS_BIAS[p] * _news_rate() for p in PAIR_CFG}

# ---------------- AI 玩家 ----------------
# 每个策略的 risk 是单笔最多投入的自由保证金比例，leverage 是可选杠杆列表中的位置（0~1）。
# take_profit / stop_loss 都是「保证金收益率」，不是价格涨跌幅。
AI_PERSONAS = {
    "trend": {
        "label": "趋势猎手",
        "risk": 0.16,
        "leverage": 0.65,
        "cooldown": (35, 90),
        "hold": (80, 220),
        "max_positions": 2,
        "take_profit": 1.20,
        "stop_loss": 0.55,
        "trend_short": 20,
        "trend_long": 100,
        "trend_signal": 0.00018,
    },
    "news": {
        "label": "新闻赌徒",
        "risk": 0.22,
        "leverage": 0.80,
        "cooldown": (18, 55),
        "hold": (20, 80),
        "max_positions": 3,
        "take_profit": 0.90,
        "stop_loss": 0.55,
    },
    "fade": {
        "label": "均值猎手",
        "risk": 0.16,
        "leverage": 0.45,
        "cooldown": (35, 95),
        "hold": (40, 140),
        "max_positions": 2,
        "take_profit": 0.55,
        "stop_loss": 0.45,
        "fade_move": 0.0045,
        "fade_lookback": 24,
    },
    "degen": {
        "label": "梭哈狂魔",
        "risk": 0.35,
        "leverage": 1.0,
        "cooldown": (15, 45),
        "hold": (15, 90),
        "max_positions": 1,
        "take_profit": 2.0,
        "stop_loss": 0.90,
    },
    "steady": {
        "label": "稳健老王",
        "risk": 0.10,
        "leverage": 0.15,
        "cooldown": (90, 220),
        "hold": (120, 360),
        "max_positions": 2,
        "take_profit": 0.40,
        "stop_loss": 0.30,
        "trend_short": 30,
        "trend_long": 140,
        "trend_signal": 0.00035,
    },
}

AI_EXIT_LABELS = {
    "ai": "平仓",
    "ai_tp": "止盈",
    "ai_sl": "止损",
    "ai_time": "到期",
    "ai_flip": "反手",
    "stopout": "强平",
    "bust": "爆仓",
}


def _pair_label(pair: str) -> str:
    return f"{pair[:3]}/{pair[3:]}"


@dataclass
class Position:
    id: int
    user_id: int
    pair: str
    side: int
    lots: float
    entry: float
    leverage: float
    opened_at: float
    opened_tick: int = 0

    @property
    def notional(self) -> float:
        return self.entry * self.lots * CONTRACT

    @property
    def margin(self) -> float:
        return self.notional / self.leverage


@dataclass
class AIPlayer:
    uid: int
    name: str
    persona: str
    created_at: float
    cooldown: int = 0
    last_news_id: int = 0
    last_action: str = "刚入场，正在观察"
    trades: list[dict] = field(default_factory=list)
    curve: list[dict] = field(default_factory=list)


class TradeError(Exception):
    pass


class Engine:
    def __init__(self) -> None:
        self.prices = {p: c["mid"] for p, c in PAIR_CFG.items()}
        self.history = {p: [] for p in PAIR_CFG}
        self.accounts: dict[int, dict] = {}
        self.positions: dict[int, Position] = {}
        self.subs: dict[asyncio.Queue, int] = {}
        self.alerts: dict[int, list[str]] = {}
        self.warned: set[int] = set()
        self.lock = asyncio.Lock()
        self.ai: dict[int, AIPlayer] = {}
        self.ai_next_uid = -1
        self.ai_position_seq = 0
        self.ai_trade_seq = 0

        self.tick = 0
        self.headline = "市场开盘，等待第一个消息。"
        self.active: list[dict] = []
        self.news: deque = deque(maxlen=12)
        self.news_seq = 0
        self.next_news = 60

    # ---------------- 启动 ----------------

    def _clamp_price(self, pair: str, value: float) -> float:
        """把价格限制在锚定价附近的合理区间内，防止 0、负数或无穷大。"""
        if not math.isfinite(value) or value <= 0:
            return PAIR_ANCHOR[pair]
        x = math.log(value)
        if x < PRICE_FLOOR_LOG[pair]:
            return math.exp(PRICE_FLOOR_LOG[pair])
        if x > PRICE_CEIL_LOG[pair]:
            return math.exp(PRICE_CEIL_LOG[pair])
        return value

    def load(self) -> None:
        accounts, positions, _ = store.load_state()
        self.accounts = accounts
        self.positions = {
            r["id"]: Position(
                id=r["id"],
                user_id=r["user_id"],
                pair=r["pair"],
                side=r["side"],
                lots=r["lots"],
                entry=r["entry_price"],
                leverage=float(r.get("leverage") or 100.0),
                opened_at=r["opened_at"],
                opened_tick=0,
            )
            for r in positions
        }

        raw = store.load_meta("prices")
        if raw:
            try:
                for p, v in json.loads(raw).items():
                    if p in self.prices:
                        saved = float(v)
                        fixed = self._clamp_price(p, saved)
                        if abs(fixed - saved) > 1e-9:
                            log.warning(
                                "price %s=%.4f out of range, clamped to %.4f",
                                p, saved, fixed,
                            )
                        self.prices[p] = fixed
            except (ValueError, TypeError):
                pass

        try:
            self.tick = int(store.load_meta("tick", "0") or 0)
        except ValueError:
            self.tick = 0

        raw = store.load_meta("news")
        if raw:
            try:
                data = json.loads(raw)
                self.news_seq = int(data.get("seq") or 0)
                self.news = deque(data.get("items") or [], maxlen=12)
            except (ValueError, TypeError):
                pass
        self.next_news = self.tick + 60
        for p in PAIR_CFG:
            self.seed_history(p)
        self.load_ai()

    def seed_history(self, pair: str, n: int = 240) -> None:
        sigma = PAIR_CFG[pair]["vol"] / math.sqrt(1440.0) * 4.0
        walk = [0.0]
        for _ in range(n - 1):
            walk.append(walk[-1] + random.gauss(0.0, sigma))
        shift = math.log(self.prices[pair]) - walk[-1]
        # 历史点也限制在合理区间内，保证图表和实时价格使用同一套边界
        self.history[pair] = [
            self._clamp_price(pair, math.exp(w + shift)) for w in walk
        ]

    def ensure_account(self, uid: int) -> None:
        if uid not in self.accounts:
            acc = store.get_account(uid)
            if acc:
                self.accounts[uid] = acc

    def config_view(self) -> dict:
        return {
            "leverages": [round(x, 2) for x in LEVERAGES],
            "min_amount": MIN_AMOUNT,
            "max_amount": MAX_AMOUNT,
            "max_positions": MAX_POSITIONS,
            "goal": GOAL,
            "contract": CONTRACT,
            "candle_ticks": CANDLE_TICKS,
            "stopout": STOPOUT,
            "warn": WARN,
            "topup_target": TOPUP_TARGET,
            "price_band": round(MAX_DEVIATION, 3),
            "price_halflife": round(PRICE_HALFLIFE, 1),
            "max_ai": MAX_AI,
        }

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                async with self.lock:
                    self.step()
                await self.broadcast()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick failed")

    def step(self) -> None:
        self.tick += 1
        self.move_prices()
        self.maybe_news()
        self.run_ai()
        self.check_risk()
        if self.tick % 30 == 0:
            self.snapshot()

    # ---------------- 行情 ----------------

    def px(self, pair: str) -> float:
        return round(self.prices[pair], 3)

    def prices_view(self) -> dict[str, float]:
        return {p: round(self.prices[p], 3) for p in PAIR_CFG}

    def move_prices(self) -> None:
        drift = {p: 0.0 for p in PAIR_CFG}
        vmult = {p: 1.0 for p in PAIR_CFG}

        had_impact = bool(self.active)

        for imp in self.active:
            if imp["left"] <= 0:
                continue
            drift[imp["pair"]] += imp["drift"]
            vmult[imp["pair"]] *= imp["vol"]
            imp["left"] -= 1

        self.active = [i for i in self.active if i["left"] > 0]

        for pair, cfg in PAIR_CFG.items():
            sigma = cfg["vol"] / math.sqrt(1440.0) * 4.0
            shock = sigma * vmult[pair] * random.gauss(0.0, 1.0)

            x = math.log(self.prices[pair])
            # 均值回复：价格偏离锚点越远，回拉越强
            x -= REVERSION * (x - ANCHOR_LOG[pair])
            # 抵消新闻库的期望偏置，否则长期会单边走成 0 或无穷大
            x += drift[pair] - NEWS_BIAS_PER_TICK[pair]
            x += shock
            x = min(max(x, PRICE_FLOOR_LOG[pair]), PRICE_CEIL_LOG[pair])
            self.prices[pair] = math.exp(x)

            self.history[pair].append(self.prices[pair])
            if len(self.history[pair]) > 300:
                del self.history[pair][0]

        if had_impact and not self.active:
            self.headline = "消息影响消退，行情回归平静。"

    def maybe_news(self) -> None:
        if NEWS_ON and self.tick >= self.next_news:
            self.start_news()

    def start_news(self) -> None:
        tpl = pick_news()

        pair_map: dict[str, float] = dict(tpl["pairs"])
        if tpl.get("random_pair"):
            pair_map = {random.choice(list(PAIR_CFG)): -1.0}
        if not pair_map:
            return

        lo, hi = NEWS_SIZE[tpl["size"]]
        move = random.uniform(lo, hi)
        ticks = {"small": 4, "mid": 5, "large": 6, "xxl": 3}[tpl["size"]]

        for pair, k in pair_map.items():
            direction = 1 if k > 0 else -1
            size = move * abs(k)

            # 消息瞬间跳空（立刻做区间约束，避免一根针穿出边界）
            self.prices[pair] = self._clamp_price(
                pair, self.prices[pair] * (1.0 + direction * size * NEWS_GAP)
            )

            rest = size * (1.0 - NEWS_GAP)
            self.active.append({
                "pair": pair,
                "left": ticks,
                "drift": math.log(1.0 + direction * rest) / ticks,
                "vol": 1.0 + 2.0 * min(1.0, size / 0.02),
            })

        self.news_seq += 1
        self.news.append({
            "id": self.news_seq,
            "tick": self.tick,
            "tag": tpl["tag"],
            "size": tpl["size"],
            "title": tpl["title"],
            "body": tpl["body"],
            "impact": [
                {"pair": p, "dir": 1 if k > 0 else -1}
                for p, k in sorted(pair_map.items(), key=lambda x: -abs(x[1]))
            ],
        })

        self.headline = f"【{tpl['tag']}】{tpl['title']}"
        self.next_news = self.tick + int(
            random.uniform(max(10.0, NEWS_MIN), max(NEWS_MIN + 10.0, NEWS_MAX))
        )

    def news_view(self) -> list[dict]:
        return list(reversed(self.news))[:8]

    # ---------------- AI 玩家 ----------------

    def is_ai(self, uid: int) -> bool:
        return uid in self.ai

    def _new_ai_uid(self) -> int:
        while self.ai_next_uid in self.accounts or self.ai_next_uid in self.ai:
            self.ai_next_uid -= 1
        uid = self.ai_next_uid
        self.ai_next_uid -= 1
        return uid

    def _unique_ai_name(self, name: str) -> str:
        base = (name or "").strip()[:24] or "AI·无名"
        names = {a.name for a in self.ai.values()}
        if base not in names:
            return base
        i = 2
        while True:
            suffix = f" {i}"
            candidate = (
                base[:24 - len(suffix)] + suffix
                if len(base) + len(suffix) > 24
                else base + suffix
            )
            if candidate not in names:
                return candidate
            i += 1

    def load_ai(self) -> None:
        """只恢复 AI 名单和策略；余额与持仓是每个服务进程重新开始的。"""
        if MAX_AI <= 0:
            return
        self.ai.clear()
        self.ai_next_uid = -1
        raw = store.load_meta("ai_players")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(data, list):
            return
        for item in data:
            if len(self.ai) >= MAX_AI:
                break
            if not isinstance(item, dict):
                continue
            persona = item.get("persona")
            if not isinstance(persona, str) or persona not in AI_PERSONAS:
                continue
            base = AI_PERSONAS[persona]["label"]
            name = self._unique_ai_name(str(item.get("name") or f"AI·{base}"))
            try:
                created_at = float(item.get("created_at") or time.time())
            except (TypeError, ValueError):
                created_at = time.time()
            self._create_ai(name, persona, created_at, random.randint(5, 20))

    def _create_ai(self, name: str, persona: str, created_at: float, cooldown: int) -> AIPlayer:
        uid = self._new_ai_uid()
        self.accounts[uid] = {
            "user_id": uid,
            "balance": START_BALANCE,
            "start_balance": START_BALANCE,
            "best_equity": START_BALANCE,
            "status": "active",
            "updated_at": time.time(),
        }
        ai = AIPlayer(
            uid=uid,
            name=self._unique_ai_name(name),
            persona=persona,
            created_at=created_at,
            cooldown=max(0, int(cooldown)),
            last_news_id=self.news_seq,
        )
        self.ai[uid] = ai
        return ai

    def add_ai(self, persona: Optional[str] = None) -> AIPlayer:
        if MAX_AI <= 0:
            raise TradeError("AI 玩家功能已关闭")
        if len(self.ai) >= MAX_AI:
            raise TradeError(f"最多只能添加 {MAX_AI} 个 AI 玩家")
        if persona is None:
            persona = random.choice(list(AI_PERSONAS))
        elif persona not in AI_PERSONAS:
            raise TradeError("未知的 AI 策略")

        label = AI_PERSONAS[persona]["label"]
        name = self._unique_ai_name(f"AI·{label}")
        ai = self._create_ai(name, persona, time.time(), random.randint(5, 20))
        self._persist_ai()
        log.info("ai added: %s (%s)", ai.name, persona)
        return ai

    def remove_ai(self, ai_uid: int) -> str:
        ai = self.ai.get(ai_uid)
        if not ai:
            raise TradeError("AI 玩家不存在")
        for pid in [p.id for p in self.positions.values() if p.user_id == ai_uid]:
            del self.positions[pid]
        self.accounts.pop(ai_uid, None)
        self.ai.pop(ai_uid, None)
        self.warned.discard(ai_uid)
        self._persist_ai()
        return ai.name

    def clear_ai(self) -> int:
        count = len(self.ai)
        for uid in list(self.ai):
            for pid in [p.id for p in self.positions.values() if p.user_id == uid]:
                del self.positions[pid]
            self.accounts.pop(uid, None)
            self.warned.discard(uid)
        self.ai.clear()
        self._persist_ai()
        return count

    def _persist_ai(self) -> None:
        rows = [
            {"name": a.name, "persona": a.persona, "created_at": a.created_at}
            for a in self.ai.values()
        ]
        store.save_meta("ai_players", json.dumps(rows, ensure_ascii=False))

    def ai_uid_by_name(self, name: str) -> Optional[int]:
        for uid, ai in self.ai.items():
            if ai.name == name:
                return uid
        return None

    def ai_view(self, uid: int) -> dict:
        ai = self.ai.get(uid)
        acc = self.accounts.get(uid)
        if not ai or not acc:
            return {}
        equity, used = self.account_equity(uid)
        return {
            "uid": uid,
            "name": ai.name,
            "persona": ai.persona,
            "persona_label": AI_PERSONAS[ai.persona]["label"],
            "balance": round(acc["balance"]),
            "equity": round(equity),
            "margin": round(used),
            "status": acc["status"],
            "last_action": ai.last_action,
        }

    def run_ai(self) -> None:
        for uid, ai in list(self.ai.items()):
            acc = self.accounts.get(uid)
            if not acc or acc["status"] != "active":
                continue
            if ai.cooldown > 0:
                ai.cooldown -= 1
            try:
                self._ai_manage(uid, ai)
                if ai.cooldown <= 0:
                    self._ai_decide(uid, ai)
            except Exception:
                log.exception("ai %s failed", ai.name)

    def _ai_manage(self, uid: int, ai: AIPlayer) -> None:
        cfg = AI_PERSONAS[ai.persona]
        for pos in [p for p in self.positions.values() if p.user_id == uid]:
            price = self.px(pos.pair)
            margin_return = (price - pos.entry) / pos.entry * pos.leverage * pos.side
            age = max(0, self.tick - pos.opened_tick) if pos.opened_tick else 0
            reason = None

            if margin_return <= -cfg["stop_loss"]:
                reason = "ai_sl"
            elif margin_return >= cfg["take_profit"]:
                reason = "ai_tp"
            elif age >= cfg["hold"][1]:
                reason = "ai_time"
            elif ai.persona == "trend" and age > 20:
                signal = self._ai_trend_signal(
                    pos.pair, cfg["trend_short"], cfg["trend_long"]
                )
                if signal * pos.side < -cfg["trend_signal"]:
                    reason = "ai_flip"

            if reason:
                self.close_position(pos.id, uid=uid, reason=reason)

    def _ai_decide(self, uid: int, ai: AIPlayer) -> None:
        cfg = AI_PERSONAS[ai.persona]
        equity, used = self.account_equity(uid)
        free = equity - used
        own = [p for p in self.positions.values() if p.user_id == uid]

        if free < MIN_AMOUNT or len(own) >= cfg["max_positions"]:
            ai.cooldown = random.randint(5, 20)
            return

        pick = self._ai_pick(ai, cfg, own)
        if not pick:
            ai.cooldown = random.randint(8, 22)
            return

        pair, side, _ = pick
        if any(p.pair == pair for p in own):
            ai.cooldown = random.randint(5, 15)
            return

        amount = math.floor(min(free * cfg["risk"], free * 0.9) / 1000.0) * 1000.0
        if amount < MIN_AMOUNT:
            ai.cooldown = random.randint(8, 22)
            return
        amount = min(amount, MAX_AMOUNT)
        leverage = self._ai_leverage(cfg["leverage"])

        try:
            self.open_position(uid, pair, side, amount, leverage)
        except TradeError:
            ai.cooldown = random.randint(8, 22)
            return

        ai.cooldown = random.randint(*cfg["cooldown"])
        side_text = "做多" if side > 0 else "做空"
        ai.last_action = (
            f"{side_text} {_pair_label(pair)} ¥{amount:,.0f} · {leverage:g}×"
        )

    def _ai_pick(self, ai: AIPlayer, cfg: dict, own: list[Position]):
        available = [
            p for p in PAIR_CFG
            if not any(pos.pair == p for pos in own)
        ]
        if not available:
            return None

        latest = self.news[-1] if self.news else None
        if latest and int(latest.get("id") or 0) > ai.last_news_id:
            ai.last_news_id = int(latest["id"])
            if ai.persona in ("news", "degen"):
                for impact in latest.get("impact") or []:
                    pair = impact.get("pair")
                    direction = int(impact.get("dir") or 0)
                    if pair in available and direction in (1, -1):
                        return pair, direction, "news"

        if ai.persona == "news":
            return None

        if ai.persona == "degen":
            if random.random() < 0.55:
                return None
            return random.choice(available), random.choice((1, -1)), "random"

        if ai.persona == "fade":
            look = int(cfg["fade_lookback"])
            moves = []
            for pair in available:
                hist = self.history[pair]
                if len(hist) <= look:
                    continue
                move = self.prices[pair] / hist[-1 - look] - 1.0
                moves.append((abs(move), pair, move))
            if not moves:
                return None
            size, pair, move = max(moves)
            if size < cfg["fade_move"]:
                return None
            return pair, (-1 if move > 0 else 1), "fade"

        signals = []
        for pair in available:
            signal = self._ai_trend_signal(
                pair, cfg["trend_short"], cfg["trend_long"]
            )
            signals.append((abs(signal), pair, signal))
        if not signals:
            return None
        size, pair, signal = max(signals)
        if size < cfg["trend_signal"]:
            return None
        return pair, (1 if signal > 0 else -1), "trend"

    def _ai_trend_signal(self, pair: str, short: int, long: int) -> float:
        hist = self.history.get(pair) or []
        if len(hist) < long or long <= 0:
            return 0.0
        short_avg = sum(hist[-short:]) / short
        long_avg = sum(hist[-long:]) / long
        return short_avg / long_avg - 1.0 if long_avg else 0.0

    def _ai_leverage(self, position: float) -> float:
        levels = list(LEVERAGES)
        if not levels:
            return 1.0
        index = int(round(position * (len(levels) - 1)))
        return levels[max(0, min(len(levels) - 1, index))]

    def _ai_stats(self, trades: list[dict]) -> dict:
        total = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] < 0)
        flats = total - wins - losses
        net = sum(t["pnl"] for t in trades)
        gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = sum(-t["pnl"] for t in trades if t["pnl"] < 0)
        decided = wins + losses
        best_streak = worst_streak = cur_win = cur_loss = 0
        for t in trades:
            pnl = t["pnl"]
            if pnl > 0:
                cur_win += 1
                cur_loss = 0
            elif pnl < 0:
                cur_loss += 1
                cur_win = 0
            else:
                cur_win = cur_loss = 0
            best_streak = max(best_streak, cur_win)
            worst_streak = max(worst_streak, cur_loss)
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "win_rate": round(wins / decided * 100.0, 1) if decided else 0.0,
            "net": round(net),
            "gross_win": round(gross_win),
            "gross_loss": round(gross_loss),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "best": round(max((t["pnl"] for t in trades), default=0.0)),
            "worst": round(min((t["pnl"] for t in trades), default=0.0)),
            "best_streak": best_streak,
            "worst_streak": worst_streak,
        }

    # ---------------- 交易 ----------------

    def open_position(self, uid: int, pair: str, side: int,
                      amount: float, leverage: float) -> Position:
        if pair not in PAIR_CFG:
            raise TradeError("未知货币对")
        if side not in (1, -1):
            raise TradeError("方向不正确")

        try:
            amount = float(amount)
            leverage = float(leverage)
        except (TypeError, ValueError):
            raise TradeError("金额或杠杆格式不正确")

        if leverage not in LEVERAGES:
            raise TradeError(f"杠杆只支持 {'/'.join(f'{x:g}' for x in LEVERAGES)} 倍")
        if amount < MIN_AMOUNT:
            raise TradeError(f"投入金额不能少于 ¥{MIN_AMOUNT:,.0f}")
        if amount > MAX_AMOUNT:
            raise TradeError(f"单笔投入不能超过 ¥{MAX_AMOUNT:,.0f}")

        acc = self.accounts.get(uid)
        if not acc:
            raise TradeError("账户不存在")
        if acc["status"] != "active":
            raise TradeError("账户已爆仓，请先重置")
        if sum(1 for p in self.positions.values() if p.user_id == uid) >= MAX_POSITIONS:
            raise TradeError(f"最多同时持有 {MAX_POSITIONS} 笔持仓")

        equity, used = self.account_equity(uid)
        if amount > equity - used:
            raise TradeError("可用资金不足，把金额调小一些")

        price = self.px(pair)
        notional = amount * leverage
        lots = notional / (price * CONTRACT)

        pid = (
            self._next_ai_position_id()
            if self.is_ai(uid)
            else store.insert_position(uid, pair, side, lots, price, None, None, leverage)
        )
        pos = Position(
            pid, uid, pair, side, lots, price, leverage, time.time(),
            opened_tick=self.tick,
        )
        self.positions[pid] = pos
        return pos

    def _next_ai_position_id(self) -> int:
        self.ai_position_seq -= 1
        return self.ai_position_seq

    def close_position(self, pid: int, uid: Optional[int] = None, reason: str = "manual") -> float:
        pos = self.positions.get(pid)
        if not pos:
            raise TradeError("持仓不存在")
        if uid is not None and pos.user_id != uid:
            raise TradeError("无权操作")

        price = self.px(pos.pair)
        pnl = (price - pos.entry) * pos.lots * CONTRACT * pos.side

        acc = self.accounts[pos.user_id]
        acc["balance"] += pnl

        if self.is_ai(pos.user_id):
            ai = self.ai.get(pos.user_id)
            if ai:
                self.ai_trade_seq += 1
                ai.trades.append({
                    "id": self.ai_trade_seq,
                    "pair": pos.pair,
                    "side": pos.side,
                    "lots": round(pos.lots, 4),
                    "leverage": round(pos.leverage, 2),
                    "amount": round(pos.margin),
                    "notional": round(pos.notional),
                    "entry": pos.entry,
                    "exit": price,
                    "pnl": round(pnl),
                    "reason": reason,
                    "opened_at": pos.opened_at,
                    "closed_at": time.time(),
                })
                label = AI_EXIT_LABELS.get(reason, "平仓")
                ai.last_action = (
                    f"{label} {_pair_label(pos.pair)} {pnl:+,.0f}"
                )
                # AI 长时间运行也只保留最近一段成交，避免内存无限增长
                if len(ai.trades) > 2000:
                    del ai.trades[:500]
            del self.positions[pid]
            return pnl

        store.insert_trade(
            pos.user_id, pos.pair, pos.side, pos.lots, pos.entry,
            price, pnl, reason, pos.opened_at, time.time(),
            leverage=pos.leverage,
        )
        store.delete_position(pid)
        store.save_account(pos.user_id, acc["balance"], acc["best_equity"], acc["status"])
        del self.positions[pid]
        return pnl

        # ---------------- 风控 ----------------
    def topup_need(self, uid: int) -> float:
        """把维持率补到 TOPUP_TARGET 需要的金额，向上取整到 1 万。"""
        equity, used = self.account_equity(uid)
        if used <= 0:
            return 0.0
        need = used * TOPUP_TARGET / 100.0 - equity
        if need <= 0:
            return 0.0
        return math.ceil(need / TOPUP_STEP) * TOPUP_STEP
    def liquidate_all(self, uid: int, reason: str = "stopout") -> None:
        """强制平掉该账户全部持仓，余额归零则标记爆仓。"""
        ids = [p.id for p in self.positions.values() if p.user_id == uid]
        total = 0.0
        for pid in ids:
            total += self.close_position(pid, reason=reason)
        acc = self.accounts.get(uid)
        if not acc:
            return
        if self.is_ai(uid):
            ai = self.ai.get(uid)
            if acc["balance"] <= 0:
                acc["balance"] = 0.0
                acc["status"] = "blown"
                if ai:
                    ai.last_action = "账户爆仓"
            elif ai:
                ai.last_action = "强制减仓后继续"
            self.warned.discard(uid)
            return
        tag = "盈利" if total >= 0 else "亏损"
        if acc["balance"] <= 0:
            acc["balance"] = 0.0
            acc["status"] = "blown"
            text = (
                f"强制平仓 {len(ids)} 笔，{tag} ¥{abs(total):,.0f}，账户已爆仓。"
                if ids else "净值归零，账户已爆仓。"
            )
        else:
            text = (
                f"维持率跌破 {STOPOUT:g}%，已强制平仓 {len(ids)} 笔，"
                f"{tag} ¥{abs(total):,.0f}。"
            )
        store.save_account(uid, acc["balance"], acc["best_equity"], acc["status"])
        self.alerts.setdefault(uid, []).append(text)
        self.warned.discard(uid)
    def check_risk(self) -> None:
        """每个 tick 检查：先强平，再预警。"""
        for uid, acc in list(self.accounts.items()):
            if acc["status"] != "active":
                continue
            equity, used = self.account_equity(uid)
            # 1. 触及爆仓线 → 全部强平
            if used > 0 and equity <= used * STOPOUT / 100.0:
                self.liquidate_all(uid, "stopout")
                continue
            # 2. 没有持仓但钱也没了 → 爆仓
            if used <= 0 and equity <= 0:
                self.liquidate_all(uid, "bust")
                continue
            # AI 不需要补仓提示
            if self.is_ai(uid):
                continue
            # 3. 进入预警区 → 提示一次，回到安全区后重新武装
            if used > 0:
                level = equity / used * 100.0
                if level < WARN and uid not in self.warned:
                    self.warned.add(uid)
                    need = self.topup_need(uid)
                    tip = f"维持率 {level:.0f}%，接近爆仓线 {STOPOUT:g}%。"
                    tip += (
                        f"建议追加保证金 ¥{need:,.0f}，或减仓。"
                        if need > 0 else "建议尽快减仓或平仓。"
                    )
                    self.alerts.setdefault(uid, []).append(tip)
                elif level > WARN * 1.15 and uid in self.warned:
                    self.warned.discard(uid)
    def deposit(self, uid: int, amount: float) -> dict:
        """追加保证金：同时提高余额和累计投入。"""
        acc = self.accounts.get(uid)
        if not acc:
            raise TradeError("账户不存在")
        if self.is_ai(uid):
            raise TradeError("AI 账户由系统管理")
        if acc["status"] != "active":
            raise TradeError("账户已爆仓，请先重置")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise TradeError("金额格式不正确")
        if amount <= 0:
            raise TradeError("追加金额必须大于 0")
        already = acc["start_balance"] - START_BALANCE
        left = MAX_DEPOSIT - already
        if left <= 0:
            raise TradeError(f"已达到累计追加上限 ¥{MAX_DEPOSIT:,.0f}")
        if amount > left:
            raise TradeError(f"最多还能追加 ¥{left:,.0f}")
        acc["balance"] += amount
        acc["start_balance"] += amount
        store.save_deposit(uid, acc["balance"], acc["start_balance"])
        self.warned.discard(uid)
        return {
            "ok": True,
            "balance": round(acc["balance"]),
            "deposited": round(already + amount),
            "max_deposit": MAX_DEPOSIT,
        }

    def reset_account(self, uid: int) -> None:
        if self.is_ai(uid):
            raise TradeError("AI 账户由系统管理")
        if uid not in self.accounts:
            raise TradeError("账户不存在")

        store.reset_account(uid)
        for pid in [p.id for p in self.positions.values() if p.user_id == uid]:
            del self.positions[pid]

        self.accounts[uid].update(
            balance=START_BALANCE,
            start_balance=START_BALANCE,
            best_equity=START_BALANCE,
            status="active",
        )
        self.alerts.pop(uid, None)
        self.warned.discard(uid)

    # ---------------- 账户 ----------------

    def position_pnl(self, p: Position) -> float:
        return (self.px(p.pair) - p.entry) * p.lots * CONTRACT * p.side

    def account_equity(self, uid: int) -> tuple[float, float]:
        acc = self.accounts.get(uid)
        if not acc:
            return 0.0, 0.0
        upnl = 0.0
        used = 0.0
        for p in self.positions.values():
            if p.user_id != uid:
                continue
            upnl += self.position_pnl(p)
            used += p.margin
        return acc["balance"] + upnl, used

    def account_view(self, uid: int) -> dict:
        acc = self.accounts.get(uid)
        if not acc:
            return {}
        equity, used = self.account_equity(uid)
        level = round(equity / used * 100.0, 1) if used > 0 else None
        return {
            "balance": round(acc["balance"]),
            "equity": round(equity),
            "margin": round(used),
            "free_margin": round(equity - used),
            "margin_level": level,
            "status": acc["status"],
            "start_balance": round(acc["start_balance"]),
            "deposited": round(acc["start_balance"] - START_BALANCE),
            "best_equity": round(acc["best_equity"]),
            "goal": GOAL,
            "topup_need": round(self.topup_need(uid)),
            "stopout": STOPOUT,
            "warn": WARN,
            "max_deposit": MAX_DEPOSIT,
        }

    def positions_view(self, uid: int) -> list[dict]:
        return [
            {
                "id": p.id,
                "pair": p.pair,
                "side": p.side,
                "amount": round(p.margin),
                "leverage": round(p.leverage, 2),
                "notional": round(p.notional),
                "lots": round(p.lots, 4),
                "entry": p.entry,
                "price": self.px(p.pair),
                "pnl": round(self.position_pnl(p)),
            }
            for p in self.positions.values()
            if p.user_id == uid
        ]
    
    def trades_view(self, uid: int, limit: int = 10, offset: int = 0) -> dict:
        ai = self.ai.get(uid)
        if ai:
            rows = list(reversed(ai.trades))[offset:offset + limit]
            return {
                "total": len(ai.trades),
                "rows": rows,
                "stats": self._ai_stats(ai.trades),
            }
        rows = []
        for r in store.trade_page(uid, limit, offset):
            lev = float(r.get("leverage") or 100.0)
            amount = r["entry_price"] * r["lots"] * CONTRACT / lev
            rows.append({
                "id": r["id"],
                "pair": r["pair"],
                "side": r["side"],
                "lots": round(r["lots"], 4),
                "leverage": round(lev, 2),
                "amount": round(amount),
                "notional": round(amount * lev),
                "entry": r["entry_price"],
                "exit": r["exit_price"],
                "pnl": round(r["pnl"]),
                "reason": r["reason"],
                "opened_at": r["opened_at"],
                "closed_at": r["closed_at"],
            })
        return {
            "total": store.trade_count(uid),
            "rows": rows,
            "stats": store.trade_stats(uid),
        }

    def profile_view(self, uid: int, viewer: Optional[int] = None) -> dict:
        acc = self.accounts.get(uid)
        if not acc:
            return {}
        ai = self.ai.get(uid)
        if ai:
            info = {"username": ai.name, "created_at": ai.created_at}
        else:
            info = store.user_info(uid) or {}
        equity, used = self.account_equity(uid)
        base = acc["start_balance"] or START_BALANCE
        ranked = sorted(
            self.accounts,
            key=lambda k: (self.account_equity(k)[0], self.accounts[k]["best_equity"]),
            reverse=True,
        )
        rank = ranked.index(uid) + 1 if uid in ranked else None
        recent = self.trades_view(uid, 5, 0)
        return {
            "name": info.get("username", f"user{uid}"),
            "joined_at": info.get("created_at"),
            "rank": rank,
            "total_players": len(ranked),
            "equity": round(equity),
            "balance": round(acc["balance"]),
            "best": round(acc["best_equity"]),
            "principal": round(acc["start_balance"]),
            "deposited": round(acc["start_balance"] - START_BALANCE),
            "margin": round(used),
            "margin_level": round(equity / used * 100.0, 1) if used > 0 else None,
            "return_pct": round((equity / base - 1.0) * 100.0, 1) if base else 0.0,
            "open_positions": sum(1 for p in self.positions.values() if p.user_id == uid),
            "status": acc["status"],
            "stats": recent["stats"],
            "trades": recent["rows"],
            "trade_total": recent["total"],
            "curve": list(ai.curve) if ai else store.equity_series(uid, 120),
            "is_ai": ai is not None,
            "persona": ai.persona if ai else None,
            "persona_label": AI_PERSONAS[ai.persona]["label"] if ai else None,
            "me": uid == viewer,
        }
    
    def leaderboard(self, viewer: Optional[int] = None) -> list[dict]:
        names = store.usernames()
        rows = []
        for uid, acc in self.accounts.items():
            equity, _ = self.account_equity(uid)
            base = acc["start_balance"] or START_BALANCE
            ai = self.ai.get(uid)
            rows.append({
                "name": ai.name if ai else names.get(uid, f"user{uid}"),
                "equity": round(equity),
                "best": round(acc["best_equity"]),
                "return_pct": round((equity / base - 1.0) * 100.0, 1) if base else 0.0,
                "status": acc["status"],
                "me": uid == viewer,
                "is_ai": ai is not None,
                "persona": ai.persona if ai else None,
                "persona_label": AI_PERSONAS[ai.persona]["label"] if ai else None,
                "note": ai.last_action if ai else "",
            })
        rows.sort(key=lambda r: (r["equity"], r["best"]), reverse=True)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

    # ---------------- 定时存档 ----------------

    def snapshot(self) -> None:
        now = time.time()
        for uid, acc in list(self.accounts.items()):
            equity, _ = self.account_equity(uid)
            if equity > acc["best_equity"]:
                acc["best_equity"] = equity
            if self.is_ai(uid):
                ai = self.ai.get(uid)
                if ai:
                    ai.curve.append({"ts": now, "equity": round(equity)})
                    if len(ai.curve) > 120:
                        del ai.curve[:-120]
                continue
            store.snapshot_equity(uid, now, equity)
            store.save_account(uid, acc["balance"], acc["best_equity"], acc["status"])

        store.save_meta("prices", json.dumps(self.prices))
        store.save_meta("tick", str(self.tick))
        store.save_meta(
            "news",
            json.dumps({"seq": self.news_seq, "items": list(self.news)}),
        )

        if self.tick % 300 == 0:
            for uid in list(self.accounts):
                if self.is_ai(uid):
                    continue
                store.prune_equity(uid, 1500)

    # ---------------- WebSocket ----------------

    def subscribe(self, uid: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.subs[q] = uid
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.pop(q, None)

    async def broadcast(self) -> None:
        if not self.subs:
            return
        prices = self.prices_view()
        for q, uid in list(self.subs.items()):
            try:
                q.put_nowait({
                    "type": "tick",
                    "tick": self.tick,
                    "prices": prices,
                    "headline": self.headline,
                    "account": self.account_view(uid),
                    "positions": self.positions_view(uid),
                    "news": self.news_view(),
                    "alerts": self.alerts.pop(uid, []),
                })
            except asyncio.QueueFull:
                pass