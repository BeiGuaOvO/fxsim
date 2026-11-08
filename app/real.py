from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from . import real_store
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

log = logging.getLogger("fx.real")

PAIRS = list(PAIR_CFG)
REAL_START_BALANCE = real_store.START_BALANCE


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


REAL_POLL_SECONDS = max(3.0, _env_float("FX_REAL_POLL_SECONDS", 10.0))
REAL_STALE_SECONDS = max(30.0, _env_float("FX_REAL_STALE_SECONDS", 120.0))
REAL_TICKS = max(200, int(_env_float("FX_REAL_TICKS", 3000.0)))
REAL_HISTORY = os.getenv("FX_REAL_HISTORY", "1") != "0"
SWISSQUOTE_ON = os.getenv("FX_REAL_SWISSQUOTE", "1") != "0"
FRANKFURTER_ON = os.getenv("FX_REAL_FRANKFURTER", "1") != "0"

USER_AGENT = "Mozilla/5.0 (compatible; FXSurvivor/1.0; +https://fx.beiguaovo.top)"
SWISSQUOTE_URL = (
    "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/"
    "{base}/{quote}"
)
FRANKFURTER_URL = (
    "https://api.frankfurter.app/latest?from=USD&to=EUR,GBP,AUD,CHF,JPY"
)
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?interval=1m&range=1d"
)


@dataclass
class RealPosition:
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


def _get_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_swissquote(pair: str, data) -> Optional[dict]:
    """Swissquote 返回多个场所/点差档位的报价，取中间价的中位数。"""
    if not isinstance(data, list):
        return None
    mids: list[float] = []
    latest_ts = 0
    for venue in data:
        if not isinstance(venue, dict):
            continue
        try:
            latest_ts = max(latest_ts, int(venue.get("ts") or 0))
        except (TypeError, ValueError):
            pass
        for spread in venue.get("spreadProfilePrices") or []:
            if not isinstance(spread, dict):
                continue
            try:
                bid = float(spread["bid"])
                ask = float(spread["ask"])
            except (KeyError, TypeError, ValueError):
                continue
            if bid > 0 and ask > 0 and ask >= bid * 0.5:
                mids.append((bid + ask) / 2.0)
    if not mids:
        return None
    price = statistics.median(mids)
    if not math.isfinite(price) or not (0.0001 < price < 10_000_000):
        return None
    ts = latest_ts / 1000.0 if latest_ts > 100_000_000_000 else float(latest_ts)
    return {"pair": pair, "price": price, "ts": ts, "source": "Swissquote"}


def parse_frankfurter(data) -> dict[str, dict]:
    """ECB 日线参考价回退：用 USD 基准换算所有 JPY 交叉盘。"""
    if not isinstance(data, dict):
        return {}
    rates = data.get("rates")
    if not isinstance(rates, dict):
        return {}
    try:
        jpy = float(rates["JPY"])
    except (KeyError, TypeError, ValueError):
        return {}
    if not math.isfinite(jpy) or jpy <= 0:
        return {}

    try:
        day = datetime.strptime(str(data.get("date")), "%Y-%m-%d")
        ts = day.replace(hour=15, minute=0, tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        ts = 0.0

    out: dict[str, dict] = {}
    if jpy > 0:
        out["USDJPY"] = {
            "pair": "USDJPY", "price": jpy, "ts": ts,
            "source": "Frankfurter 日线",
        }
    for base in ("EUR", "GBP", "AUD", "CHF"):
        pair = f"{base}JPY"
        try:
            rate = float(rates[base])
        except (KeyError, TypeError, ValueError):
            continue
        if rate > 0:
            out[pair] = {
                "pair": pair, "price": jpy / rate, "ts": ts,
                "source": "Frankfurter 日线",
            }
    return out


def parse_yahoo_history(pair: str, data) -> list[tuple[float, float]]:
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
    return out[-600:]


class RealEngine:
    def __init__(self) -> None:
        self.prices: dict[str, Optional[float]] = {p: None for p in PAIRS}
        self.quotes: dict[str, dict] = {}
        self.ticks: dict[str, deque] = {
            p: deque(maxlen=REAL_TICKS) for p in PAIRS
        }
        self.accounts: dict[int, dict] = {}
        self.positions: dict[int, RealPosition] = {}
        self.alerts: dict[int, list[str]] = {}
        self.warned: set[int] = set()
        self.lock = asyncio.Lock()

        self.last_update = 0.0
        self.source = ""
        self.last_error = ""
        self.poll_count = 0

    # ---------------- 启动 / 轮询 ----------------

    def load(self) -> None:
        accounts, positions = real_store.load_state()
        self.accounts = accounts
        self.positions = {
            r["id"]: RealPosition(
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

    def ensure_account(self, uid: int) -> dict:
        if uid not in self.accounts:
            self.accounts[uid] = real_store.ensure_account(uid)
        return self.accounts[uid]

    async def run(self) -> None:
        await self._poll_once()
        if REAL_HISTORY:
            try:
                history = await asyncio.to_thread(self.fetch_history)
                if history:
                    async with self.lock:
                        self.apply_history(history)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("real history failed")
        while True:
            await asyncio.sleep(REAL_POLL_SECONDS)
            await self._poll_once()

    async def _poll_once(self) -> None:
        try:
            quotes, error = await asyncio.to_thread(self.fetch_quotes)
            async with self.lock:
                if quotes:
                    self.apply_quotes(quotes)
                    self.last_error = ""
                else:
                    self.last_error = error or "行情源不可用"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("real poll failed")
            async with self.lock:
                self.last_error = f"{type(exc).__name__}: {exc}"

    # ---------------- 数据源 ----------------

    def fetch_quotes(self) -> tuple[dict[str, dict], str]:
        quotes: dict[str, dict] = {}
        if SWISSQUOTE_ON:
            with ThreadPoolExecutor(max_workers=len(PAIRS)) as pool:
                results = list(pool.map(self._fetch_swissquote, PAIRS))
            for quote in results:
                if quote:
                    quotes[quote["pair"]] = quote

        missing = [p for p in PAIRS if p not in quotes]
        if missing and FRANKFURTER_ON:
            daily = self._fetch_frankfurter()
            for pair in missing:
                if pair in daily:
                    quotes[pair] = daily[pair]

        if not quotes:
            return {}, "Swissquote / Frankfurter 均不可用"
        return quotes, ""

    def _fetch_swissquote(self, pair: str) -> Optional[dict]:
        base, quote = pair[:3], pair[3:]
        url = SWISSQUOTE_URL.format(base=base, quote=quote)
        try:
            return parse_swissquote(pair, _get_json(url, timeout=8.0))
        except Exception as exc:
            log.debug("swissquote %s failed: %s", pair, exc)
            return None

    def _fetch_frankfurter(self) -> dict[str, dict]:
        try:
            return parse_frankfurter(_get_json(FRANKFURTER_URL, timeout=10.0))
        except Exception as exc:
            log.debug("frankfurter failed: %s", exc)
            return {}

    def fetch_history(self) -> dict[str, list[tuple[float, float]]]:
        out: dict[str, list[tuple[float, float]]] = {}
        with ThreadPoolExecutor(max_workers=len(PAIRS)) as pool:
            results = list(pool.map(self._fetch_yahoo_history, PAIRS))
        for pair, points in zip(PAIRS, results):
            if points:
                out[pair] = points
        return out

    def _fetch_yahoo_history(self, pair: str) -> list[tuple[float, float]]:
        symbol = f"{pair}=X"
        try:
            data = _get_json(YAHOO_URL.format(symbol=symbol), timeout=12.0)
            return parse_yahoo_history(pair, data)
        except Exception as exc:
            log.debug("yahoo history %s failed: %s", pair, exc)
            return []

    # ---------------- 状态更新 ----------------

    def apply_history(self, history: dict[str, list[tuple[float, float]]]) -> None:
        for pair, points in history.items():
            if pair not in self.ticks or not points:
                continue
            live = self.quotes.get(pair)
            if live and live.get("source") != "Yahoo 历史":
                try:
                    cutoff = float(live.get("ts") or 0)
                except (TypeError, ValueError):
                    cutoff = 0.0
                if cutoff > 0:
                    # 只用当前实时报价之前的历史，让图表和最新报价对齐
                    points = [(t, p) for t, p in points if t <= cutoff + 1.0]
            if not points:
                continue

            existing = list(self.ticks[pair])
            merged: dict[float, float] = {t: p for t, p in points}
            # 实时 tick 覆盖同时间的历史点，然后按时间排序
            merged.update({t: p for t, p in existing})
            ordered = sorted(merged.items())[-REAL_TICKS:]
            self.ticks[pair] = deque(ordered, maxlen=REAL_TICKS)
            if self.prices[pair] is None:
                self.prices[pair] = points[-1][1]
            self.source = "Yahoo 历史" if not self.quotes else self.source
            self.quotes.setdefault(pair, {
                "price": points[-1][1],
                "ts": points[-1][0],
                "source": "Yahoo 历史",
            })

    def apply_quotes(self, quotes: dict[str, dict]) -> None:
        live_sources: set[str] = set()
        for pair, quote in quotes.items():
            if pair not in self.ticks:
                continue
            price = float(quote["price"])
            ts = float(quote.get("ts") or time.time())
            source = str(quote.get("source") or "未知")
            live_sources.add(source)
            self.prices[pair] = price
            self.quotes[pair] = {"price": price, "ts": ts, "source": source}

            ticks = self.ticks[pair]
            if not ticks:
                ticks.append((ts, price))
            else:
                last_ts, last_price = ticks[-1]
                if ts - last_ts >= 1.0:
                    ticks.append((ts, price))
                elif price != last_price and ts >= last_ts:
                    ticks[-1] = (ts, price)
            self.last_update = max(self.last_update, ts)
            self.source = " + ".join(sorted(live_sources))

        self.poll_count += 1
        self.check_risk()
        for uid, acc in self.accounts.items():
            equity, _ = self.account_equity(uid)
            if equity > acc["best_equity"]:
                acc["best_equity"] = equity
        if self.poll_count % 30 == 0:
            self.persist_accounts()

    def persist_accounts(self) -> None:
        for uid, acc in self.accounts.items():
            real_store.save_account(
                uid, acc["balance"], acc["best_equity"], acc["status"]
            )

    # ---------------- 风控 ----------------

    def check_risk(self) -> None:
        for uid, acc in list(self.accounts.items()):
            if acc["status"] != "active":
                continue
            equity, used = self.account_equity(uid)
            if used > 0 and equity <= used * STOPOUT / 100.0:
                self.liquidate_all(uid, "stopout")
                continue
            if used <= 0 and equity <= 0:
                self.liquidate_all(uid, "bust")
                continue
            if used > 0:
                level = equity / used * 100.0
                if level < WARN and uid not in self.warned:
                    self.warned.add(uid)
                    self.alerts.setdefault(uid, []).append(
                        f"维持率 {level:.0f}%，已低于预警线 {WARN:g}%，注意减仓。"
                    )
                elif level > WARN * 1.15 and uid in self.warned:
                    self.warned.discard(uid)

    def liquidate_all(self, uid: int, reason: str = "stopout") -> None:
        ids = [p.id for p in self.positions.values() if p.user_id == uid]
        total = 0.0
        for pid in ids:
            total += self.close_position(pid, reason=reason)
        acc = self.accounts.get(uid)
        if not acc:
            return
        if acc["balance"] <= 0:
            acc["balance"] = 0.0
            acc["status"] = "blown"
        real_store.save_account(uid, acc["balance"], acc["best_equity"], acc["status"])
        if ids:
            self.alerts.setdefault(uid, []).append(
                f"真实盘触发强平 {len(ids)} 笔，合计盈亏 {total:+,.0f}。"
            )
        self.warned.discard(uid)

    # ---------------- 交易 ----------------

    def px(self, pair: str) -> Optional[float]:
        value = self.prices.get(pair)
        return round(value, 3) if value is not None else None

    def prices_view(self) -> dict[str, Optional[float]]:
        return {p: self.px(p) for p in PAIRS}

    def open_position(self, uid: int, pair: str, side: int,
                      amount: float, leverage: float) -> RealPosition:
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
            raise TradeError("真实盘账户不存在")
        if acc["status"] != "active":
            raise TradeError("真实盘账户已爆仓，请先重置")
        if sum(1 for p in self.positions.values() if p.user_id == uid) >= MAX_POSITIONS:
            raise TradeError(f"最多同时持有 {MAX_POSITIONS} 笔持仓")

        equity, used = self.account_equity(uid)
        if amount > equity - used:
            raise TradeError("可用资金不足，把金额调小一些")

        price = self.px(pair)
        if price is None:
            raise TradeError("真实行情尚未加载，请稍后再试")

        notional = amount * leverage
        lots = notional / (price * CONTRACT)
        pid = real_store.insert_position(uid, pair, side, lots, price, leverage)
        pos = RealPosition(pid, uid, pair, side, lots, price, leverage, time.time())
        self.positions[pid] = pos
        return pos

    def close_position(self, pid: int, uid: Optional[int] = None,
                       reason: str = "manual") -> float:
        pos = self.positions.get(pid)
        if not pos:
            raise TradeError("持仓不存在")
        if uid is not None and pos.user_id != uid:
            raise TradeError("无权操作")

        current = self.px(pos.pair)
        price = current if current is not None else pos.entry
        pnl = (price - pos.entry) * pos.lots * CONTRACT * pos.side
        acc = self.accounts[pos.user_id]
        acc["balance"] += pnl

        real_store.insert_trade(
            pos.user_id, pos.pair, pos.side, pos.lots, pos.entry,
            price, pnl, reason, pos.opened_at, time.time(), pos.leverage,
        )
        real_store.delete_position(pid)
        real_store.save_account(
            pos.user_id, acc["balance"], acc["best_equity"], acc["status"]
        )
        del self.positions[pid]
        return pnl

    def reset_account(self, uid: int) -> None:
        self.ensure_account(uid)
        real_store.reset_account(uid)
        for pid in [p.id for p in self.positions.values() if p.user_id == uid]:
            del self.positions[pid]
        self.accounts[uid].update(
            balance=REAL_START_BALANCE,
            start_balance=REAL_START_BALANCE,
            best_equity=REAL_START_BALANCE,
            status="active",
        )
        self.alerts.pop(uid, None)
        self.warned.discard(uid)

    # ---------------- 视图 ----------------

    def account_equity(self, uid: int) -> tuple[float, float]:
        acc = self.accounts.get(uid)
        if not acc:
            return 0.0, 0.0
        upnl = 0.0
        used = 0.0
        for pos in self.positions.values():
            if pos.user_id != uid:
                continue
            price = self.px(pos.pair)
            mark = price if price is not None else pos.entry
            upnl += (mark - pos.entry) * pos.lots * CONTRACT * pos.side
            used += pos.margin
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
            "best_equity": round(acc["best_equity"]),
            "stopout": STOPOUT,
            "warn": WARN,
        }

    def positions_view(self, uid: int) -> list[dict]:
        rows = []
        for pos in self.positions.values():
            if pos.user_id != uid:
                continue
            price = self.px(pos.pair)
            mark = price if price is not None else pos.entry
            rows.append({
                "id": pos.id,
                "pair": pos.pair,
                "side": pos.side,
                "amount": round(pos.margin),
                "leverage": round(pos.leverage, 2),
                "notional": round(pos.notional),
                "lots": round(pos.lots, 4),
                "entry": pos.entry,
                "price": mark,
                "pnl": round((mark - pos.entry) * pos.lots * CONTRACT * pos.side),
            })
        return rows

    def trades_view(self, uid: int, limit: int = 10, offset: int = 0) -> dict:
        rows = []
        for r in real_store.trade_page(uid, limit, offset):
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
            "total": real_store.trade_count(uid),
            "rows": rows,
            "stats": real_store.trade_stats(uid),
        }

    def market_view(self) -> dict:
        return {
            "source": self.source,
            "last_update": round(self.last_update, 3) if self.last_update else None,
            "stale": self.is_stale(),
            "error": self.last_error,
            "poll_seconds": REAL_POLL_SECONDS,
            "server_time": round(time.time(), 3),
        }

    def is_stale(self) -> bool:
        return (
            self.last_update <= 0
            or time.time() - self.last_update > REAL_STALE_SECONDS
        )

    def config_view(self) -> dict:
        return {
            "leverages": [round(x, 2) for x in LEVERAGES],
            "min_amount": MIN_AMOUNT,
            "max_amount": MAX_AMOUNT,
            "max_positions": MAX_POSITIONS,
            "contract": CONTRACT,
            "stopout": STOPOUT,
            "warn": WARN,
            "start_balance": round(REAL_START_BALANCE),
            "poll_seconds": REAL_POLL_SECONDS,
        }

    def state_view(self, uid: int) -> dict:
        self.ensure_account(uid)
        recent = self.trades_view(uid, 10, 0)
        return {
            "account": self.account_view(uid),
            "positions": self.positions_view(uid),
            "prices": self.prices_view(),
            "quotes": {
                p: {"price": round(q["price"], 3), "ts": q.get("ts"),
                    "source": q.get("source")}
                for p, q in self.quotes.items()
            },
            "config": self.config_view(),
            "market": self.market_view(),
            "trades": recent["rows"],
            "stats": recent["stats"],
            "trade_total": recent["total"],
            "alerts": self.alerts.pop(uid, []),
        }

    def history_view(self, pair: str, limit: int = 300) -> dict:
        points = list(self.ticks[pair])
        if limit > 0:
            points = points[-limit:]
        return {
            "pair": pair,
            "points": [[round(ts, 3), round(price, 3)] for ts, price in points],
            "last_update": round(self.last_update, 3) if self.last_update else None,
            "source": self.source,
        }
