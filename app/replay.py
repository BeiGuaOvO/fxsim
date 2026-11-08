from __future__ import annotations

import asyncio
import bisect
import json
import logging
import math
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from .engine import (
    CONTRACT,
    LEVERAGES,
    MAX_AMOUNT,
    MAX_POSITIONS,
    MIN_AMOUNT,
    PAIR_CFG,
    STOPOUT,
    WARN,
    TradeError,
)

log = logging.getLogger("fx.replay")

PAIRS = list(PAIR_CFG)
INTERVAL = "5m"
INTERVAL_SECONDS = 300
RANGE = "30d"
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?interval=" + INTERVAL + "&range=" + RANGE
)
USER_AGENT = "Mozilla/5.0 (compatible; FXSurvivor/1.0; +https://fx.beiguaovo.top)"
CACHE_SECONDS = 6 * 3600


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


START_BALANCE = max(1_000.0, _env_float("FX_REPLAY_START_BALANCE", 1_000_000.0))
DEFAULT_SPEED = max(0.2, _env_float("FX_REPLAY_DEFAULT_SPEED", 5.0))


def _parse_speeds(raw: str) -> tuple[float, ...]:
    out = []
    for part in raw.split(","):
        try:
            value = float(part.strip())
        except ValueError:
            continue
        if 0.1 <= value <= 120:
            out.append(value)
    return tuple(sorted(set(out))) or (1.0, 5.0, 10.0, 30.0, 60.0)


SPEEDS = _parse_speeds(os.getenv("FX_REPLAY_SPEEDS", "1,3,5,10,30,60"))
MAX_STEPS = 2000


class ReplayError(Exception):
    pass


@dataclass
class ReplayData:
    times: list[float]
    prices: dict[str, list[float]]
    interval: int
    source: str
    fetched_at: float


@dataclass
class ReplayPosition:
    id: int
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


@dataclass
class ReplaySession:
    uid: int
    start_idx: int
    cursor: int
    speed: float
    playing: bool
    account: dict
    positions: dict[int, ReplayPosition] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    warned: bool = False
    ended: bool = False
    last_wall: float = field(default_factory=time.time)
    pid_seq: int = 0
    trade_seq: int = 0
    created_at: float = field(default_factory=time.time)


def _get_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_yahoo_all(pair: str, data) -> list[tuple[float, float]]:
    if not isinstance(data, dict):
        return []
    result = (data.get("chart") or {}).get("result")
    if not isinstance(result, list) or not result:
        return []
    result = result[0]
    if not isinstance(result, dict):
        return []
    stamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or [{}]
    closes = (quotes[0] or {}).get("close") or []
    out: list[tuple[float, float]] = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        try:
            ts = float(ts)
            close = float(close)
        except (TypeError, ValueError):
            continue
        if ts > 0 and math.isfinite(close) and close > 0:
            out.append((ts, close))
    return out


def build_replay_data(raw: dict[str, list[tuple[float, float]]]) -> ReplayData:
    """把各货币对时间戳对齐成一条时间线，缺失的用上一个价格前向填充。"""
    maps = {pair: {float(t): float(p) for t, p in raw.get(pair, [])} for pair in PAIRS}
    if any(not maps[p] for p in PAIRS):
        raise ReplayError("历史数据不完整")

    times = sorted(set().union(*(set(m) for m in maps.values())))
    last: dict[str, float] = {}
    aligned_times: list[float] = []
    aligned: dict[str, list[float]] = {p: [] for p in PAIRS}
    for ts in times:
        for pair in PAIRS:
            if ts in maps[pair]:
                last[pair] = maps[pair][ts]
        if len(last) < len(PAIRS):
            continue
        aligned_times.append(ts)
        for pair in PAIRS:
            aligned[pair].append(last[pair])

    if len(aligned_times) < 2:
        raise ReplayError("历史数据不足")
    return ReplayData(
        times=aligned_times,
        prices=aligned,
        interval=INTERVAL_SECONDS,
        source="Yahoo Finance 5m",
        fetched_at=time.time(),
    )


class ReplayEngine:
    def __init__(self) -> None:
        self.data: Optional[ReplayData] = None
        self.sessions: dict[int, ReplaySession] = {}
        self.lock = asyncio.Lock()
        self._load_lock = threading.Lock()

    # ---------------- 数据加载 ----------------

    def load_data(self, force: bool = False) -> ReplayData:
        # 有活跃回放时不要替换时间线，否则会话索引会和新数据错位
        if self.sessions and self.data is not None:
            return self.data
        if (
            not force
            and self.data is not None
            and time.time() - self.data.fetched_at < CACHE_SECONDS
        ):
            return self.data
        with self._load_lock:
            if (
                not force
                and self.data is not None
                and time.time() - self.data.fetched_at < CACHE_SECONDS
            ):
                return self.data
            raw: dict[str, list[tuple[float, float]]] = {}
            with ThreadPoolExecutor(max_workers=len(PAIRS)) as pool:
                results = list(pool.map(self._fetch_pair, PAIRS))
            for pair, points in zip(PAIRS, results):
                if points:
                    raw[pair] = points
            if len(raw) < len(PAIRS):
                if self.data is not None:
                    log.warning("replay data refresh failed, keeping cache")
                    return self.data
                raise ReplayError("Yahoo 30 天历史数据暂不可用")
            data = build_replay_data(raw)
            self.data = data
            return data

    def _fetch_pair(self, pair: str) -> list[tuple[float, float]]:
        symbol = f"{pair}=X"
        try:
            data = _get_json(YAHOO_URL.format(symbol=symbol), timeout=25.0)
            return parse_yahoo_all(pair, data)
        except Exception as exc:
            log.warning("replay yahoo %s failed: %s", pair, exc)
            return []

    def meta(self) -> dict:
        data = self.data
        if data is None:
            raise ReplayError("回放数据尚未加载")
        default_start = max(data.times[0], data.times[-1] - 7 * 86400)
        return {
            "from": data.times[0],
            "to": data.times[-1],
            "default_start": default_start,
            "interval": data.interval,
            "interval_label": "5m",
            "source": data.source,
            "range": RANGE,
            "speeds": list(SPEEDS),
            "default_speed": DEFAULT_SPEED,
            "start_balance": round(START_BALANCE),
            "pairs": PAIRS,
            "leverages": [round(x, 2) for x in LEVERAGES],
            "min_amount": MIN_AMOUNT,
            "max_amount": MAX_AMOUNT,
            "max_positions": MAX_POSITIONS,
            "stopout": STOPOUT,
            "warn": WARN,
        }

    # ---------------- 会话 ----------------

    def start(self, uid: int, start_ts: Optional[float] = None,
              speed: Optional[float] = None) -> ReplaySession:
        data = self.data
        if data is None:
            raise ReplayError("回放数据尚未加载")
        if speed is None:
            speed = DEFAULT_SPEED
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            raise ReplayError("回放速度格式不正确")
        speed = min(120.0, max(0.2, speed))

        if start_ts is None:
            start_ts = data.times[-1] - 7 * 86400
        try:
            start_ts = float(start_ts)
        except (TypeError, ValueError):
            raise ReplayError("开始时间格式不正确")
        index = bisect.bisect_left(data.times, start_ts)
        index = max(0, min(len(data.times) - 2, index))

        session = ReplaySession(
            uid=uid,
            start_idx=index,
            cursor=index,
            speed=speed,
            playing=True,
            account={
                "balance": START_BALANCE,
                "start_balance": START_BALANCE,
                "best_equity": START_BALANCE,
                "status": "active",
            },
            last_wall=time.time(),
        )
        self.sessions[uid] = session
        return session

    def get_session(self, uid: int) -> Optional[ReplaySession]:
        return self.sessions.get(uid)

    def _prices(self, session: ReplaySession) -> dict[str, float]:
        data = self.data
        assert data is not None
        return {
            pair: data.prices[pair][session.cursor] for pair in PAIRS
        }

    def px(self, session: ReplaySession, pair: str) -> Optional[float]:
        price = self._prices(session).get(pair)
        return round(price, 3) if price is not None else None

    def advance(self, session: ReplaySession) -> None:
        if not session.playing or session.ended or session.account["status"] != "active":
            session.last_wall = time.time()
            return
        data = self.data
        if data is None:
            return

        now = time.time()
        elapsed = now - session.last_wall
        if elapsed <= 0:
            return
        steps = int(elapsed * session.speed)
        if steps <= 0:
            return
        steps = min(steps, MAX_STEPS)
        session.last_wall += steps / session.speed

        for _ in range(steps):
            if session.cursor >= len(data.times) - 1:
                session.ended = True
                session.playing = False
                break
            session.cursor += 1
            self._check_risk(session)
            if not session.playing or session.account["status"] != "active":
                if session.account["status"] != "active":
                    session.playing = False
                break

        equity, _ = self._account_equity(session)
        if equity > session.account["best_equity"]:
            session.account["best_equity"] = equity

    def control(self, session: ReplaySession, playing: Optional[bool] = None,
                speed: Optional[float] = None) -> None:
        if speed is not None:
            try:
                speed = float(speed)
            except (TypeError, ValueError):
                raise ReplayError("回放速度格式不正确")
            session.speed = min(120.0, max(0.2, speed))
            session.last_wall = time.time()
        if playing is not None:
            if playing and session.account["status"] != "active":
                raise ReplayError("回放账户已爆仓，请重新开始")
            if session.ended and playing:
                raise ReplayError("回放已经结束，请重新开始")
            session.playing = bool(playing)
            session.last_wall = time.time()

    def reset(self, session: ReplaySession) -> ReplaySession:
        start_ts = self.data.times[session.start_idx] if self.data else None
        return self.start(session.uid, start_ts, session.speed)

    # ---------------- 风控 ----------------

    def _check_risk(self, session: ReplaySession) -> None:
        equity, used = self._account_equity(session)
        if used > 0 and equity <= used * STOPOUT / 100.0:
            self._liquidate_all(session, "stopout")
            return
        if used <= 0 and equity <= 0:
            self._liquidate_all(session, "bust")
            return
        if used > 0:
            level = equity / used * 100.0
            if level < WARN and not session.warned:
                session.warned = True
                session.alerts.append(
                    f"维持率 {level:.0f}%，已低于预警线 {WARN:g}%，注意减仓。"
                )
            elif level > WARN * 1.15:
                session.warned = False

    def _liquidate_all(self, session: ReplaySession, reason: str) -> None:
        ids = [p.id for p in session.positions.values()]
        total = 0.0
        for pid in ids:
            total += self.close_position(session, pid, reason=reason)
        if session.account["balance"] <= 0:
            session.account["balance"] = 0.0
            session.account["status"] = "blown"
        session.alerts.append(
            f"触发强平 {len(ids)} 笔，合计盈亏 {total:+,.0f}。"
        )
        session.playing = False
        session.warned = False

    # ---------------- 交易 ----------------

    def open_position(self, session: ReplaySession, pair: str, side: int,
                      amount: float, leverage: float) -> ReplayPosition:
        if pair not in PAIR_CFG:
            raise TradeError("未知货币对")
        if side not in (1, -1):
            raise TradeError("方向不正确")
        if session.ended:
            raise TradeError("回放已经结束，请重新开始")
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
        if session.account["status"] != "active":
            raise TradeError("回放账户已爆仓，请重新开始")
        if len(session.positions) >= MAX_POSITIONS:
            raise TradeError(f"最多同时持有 {MAX_POSITIONS} 笔持仓")

        equity, used = self._account_equity(session)
        if amount > equity - used:
            raise TradeError("可用资金不足，把金额调小一些")

        price = self.px(session, pair)
        if price is None:
            raise TradeError("回放价格尚未就绪")

        notional = amount * leverage
        lots = notional / (price * CONTRACT)
        session.pid_seq -= 1
        pid = session.pid_seq
        opened_at = self.data.times[session.cursor] if self.data else time.time()
        pos = ReplayPosition(pid, pair, side, lots, price, leverage, opened_at)
        session.positions[pid] = pos
        return pos

    def close_position(self, session: ReplaySession, pid: int,
                       reason: str = "manual") -> float:
        pos = session.positions.get(pid)
        if not pos:
            raise TradeError("持仓不存在")
        price = self.px(session, pos.pair)
        if price is None:
            price = pos.entry
        pnl = (price - pos.entry) * pos.lots * CONTRACT * pos.side
        session.account["balance"] += pnl
        closed_at = self.data.times[session.cursor] if self.data else time.time()

        session.trade_seq += 1
        session.trades.append({
            "id": session.trade_seq,
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
            "closed_at": closed_at,
        })
        if len(session.trades) > 2000:
            del session.trades[:500]
        del session.positions[pid]
        return pnl

    # ---------------- 视图 ----------------

    def _account_equity(self, session: ReplaySession) -> tuple[float, float]:
        prices = self._prices(session) if self.data else {}
        upnl = 0.0
        used = 0.0
        for pos in session.positions.values():
            mark = prices.get(pos.pair, pos.entry)
            upnl += (mark - pos.entry) * pos.lots * CONTRACT * pos.side
            used += pos.margin
        return session.account["balance"] + upnl, used

    def account_view(self, session: ReplaySession) -> dict:
        equity, used = self._account_equity(session)
        level = round(equity / used * 100.0, 1) if used > 0 else None
        return {
            "balance": round(session.account["balance"]),
            "equity": round(equity),
            "margin": round(used),
            "free_margin": round(equity - used),
            "margin_level": level,
            "status": session.account["status"],
            "start_balance": round(session.account["start_balance"]),
            "best_equity": round(session.account["best_equity"]),
            "stopout": STOPOUT,
            "warn": WARN,
        }

    def positions_view(self, session: ReplaySession) -> list[dict]:
        prices = self._prices(session) if self.data else {}
        rows = []
        for pos in session.positions.values():
            mark = prices.get(pos.pair, pos.entry)
            rows.append({
                "id": pos.id,
                "pair": pos.pair,
                "side": pos.side,
                "amount": round(pos.margin),
                "leverage": round(pos.leverage, 2),
                "notional": round(pos.notional),
                "lots": round(pos.lots, 4),
                "entry": pos.entry,
                "price": round(mark, 3),
                "pnl": round((mark - pos.entry) * pos.lots * CONTRACT * pos.side),
            })
        return rows

    def _trades_view(self, session: ReplaySession, limit: int = 10) -> dict:
        rows = list(reversed(session.trades))[:limit]
        return {
            "total": len(session.trades),
            "rows": rows,
            "stats": self._trade_stats(session.trades),
        }

    def _trade_stats(self, trades: list[dict]) -> dict:
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

    def _progress(self, session: ReplaySession) -> float:
        data = self.data
        if not data:
            return 0.0
        span = len(data.times) - 1 - session.start_idx
        if span <= 0:
            return 1.0
        return max(0.0, min(1.0, (session.cursor - session.start_idx) / span))

    def state_view(self, session: ReplaySession) -> dict:
        self.advance(session)
        data = self.data
        assert data is not None
        recent = self._trades_view(session)
        prices = self._prices(session)
        alerts = list(session.alerts)
        session.alerts.clear()
        return {
            "active": True,
            "playing": session.playing,
            "ended": session.ended,
            "speed": session.speed,
            "start_ts": data.times[session.start_idx],
            "cursor_ts": data.times[session.cursor],
            "data_start": data.times[0],
            "data_end": data.times[-1],
            "progress": round(self._progress(session), 4),
            "interval": data.interval,
            "account": self.account_view(session),
            "positions": self.positions_view(session),
            "prices": {pair: round(prices[pair], 3) for pair in PAIRS},
            "trades": recent["rows"],
            "trade_total": recent["total"],
            "stats": recent["stats"],
            "alerts": alerts,
            "start_balance": round(session.account["start_balance"]),
            "source": data.source,
        }

    def history_view(self, session: ReplaySession, pair: str,
                     limit: int = 300) -> dict:
        if pair not in PAIR_CFG:
            raise ReplayError("未知货币对")
        self.advance(session)
        data = self.data
        assert data is not None
        end = session.cursor
        start = max(0, end - limit + 1)
        points = [
            [round(data.times[i], 3), round(data.prices[pair][i], 3)]
            for i in range(start, end + 1)
        ]
        return {
            "pair": pair,
            "points": points,
            "cursor_ts": data.times[session.cursor],
            "start_ts": data.times[session.start_idx],
            "data_start": data.times[0],
            "data_end": data.times[-1],
            "playing": session.playing,
            "ended": session.ended,
            "progress": round(self._progress(session), 4),
            "interval": data.interval,
        }
