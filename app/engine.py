from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
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

    @property
    def notional(self) -> float:
        return self.entry * self.lots * CONTRACT

    @property
    def margin(self) -> float:
        return self.notional / self.leverage


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

        self.tick = 0
        self.headline = "市场开盘，等待第一个消息。"
        self.active: list[dict] = []
        self.news: deque = deque(maxlen=12)
        self.news_seq = 0
        self.next_news = 60

    # ---------------- 启动 ----------------

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
            )
            for r in positions
        }

        raw = store.load_meta("prices")
        if raw:
            try:
                for p, v in json.loads(raw).items():
                    if p in self.prices:
                        self.prices[p] = float(v)
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

    def seed_history(self, pair: str, n: int = 240) -> None:
        sigma = PAIR_CFG[pair]["vol"] / math.sqrt(1440.0) * 4.0
        walk = [0.0]
        for _ in range(n - 1):
            walk.append(walk[-1] + random.gauss(0.0, sigma))
        shift = math.log(self.prices[pair]) - walk[-1]
        self.history[pair] = [math.exp(w + shift) for w in walk]

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
            self.prices[pair] *= math.exp(drift[pair] + shock)

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

            # 消息瞬间跳空
            self.prices[pair] *= 1.0 + direction * size * NEWS_GAP

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

        pid = store.insert_position(uid, pair, side, lots, price, None, None, leverage)
        pos = Position(pid, uid, pair, side, lots, price, leverage, time.time())
        self.positions[pid] = pos
        return pos

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
            "stats": store.trade_stats(uid),
            "trades": recent["rows"],
            "trade_total": recent["total"],
            "curve": store.equity_series(uid, 120),
            "me": uid == viewer,
        }
    
    def leaderboard(self, viewer: Optional[int] = None) -> list[dict]:
        names = store.usernames()
        rows = []
        for uid, acc in self.accounts.items():
            equity, _ = self.account_equity(uid)
            base = acc["start_balance"] or START_BALANCE
            rows.append({
                "name": names.get(uid, f"user{uid}"),
                "equity": round(equity),
                "best": round(acc["best_equity"]),
                "return_pct": round((equity / base - 1.0) * 100.0, 1) if base else 0.0,
                "status": acc["status"],
                "me": uid == viewer,
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