from __future__ import annotations

import os
import time
from typing import Optional

from . import store

# 真实盘使用完全独立的账户体系，和模拟盘的 30 万资金互不影响
def _start_balance() -> float:
    try:
        return max(1_000.0, float(os.getenv("FX_REAL_START_BALANCE", "1000000")))
    except ValueError:
        return 1_000_000.0


START_BALANCE = _start_balance()

SCHEMA = """
CREATE TABLE IF NOT EXISTS real_accounts (
    user_id INTEGER PRIMARY KEY,
    balance REAL NOT NULL,
    start_balance REAL NOT NULL,
    best_equity REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS real_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    pair TEXT NOT NULL,
    side INTEGER NOT NULL,
    lots REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 100,
    entry_price REAL NOT NULL,
    opened_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS real_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    pair TEXT NOT NULL,
    side INTEGER NOT NULL,
    lots REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 100,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL,
    reason TEXT NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_real_positions_user ON real_positions(user_id);
CREATE INDEX IF NOT EXISTS idx_real_trades_user ON real_trades(user_id, closed_at DESC);
"""


def init_db() -> None:
    with store.db() as c:
        c.executescript(SCHEMA)


def get_account(uid: int) -> Optional[dict]:
    with store.db() as c:
        row = c.execute(
            "SELECT * FROM real_accounts WHERE user_id = ?", (uid,)
        ).fetchone()
    return dict(row) if row else None


def ensure_account(uid: int) -> dict:
    acc = get_account(uid)
    if acc:
        return acc
    now = time.time()
    with store.db() as c:
        c.execute(
            "INSERT OR IGNORE INTO real_accounts"
            "(user_id, balance, start_balance, best_equity, status, updated_at) "
            "VALUES(?, ?, ?, ?, 'active', ?)",
            (uid, START_BALANCE, START_BALANCE, START_BALANCE, now),
        )
    return get_account(uid) or {
        "user_id": uid,
        "balance": START_BALANCE,
        "start_balance": START_BALANCE,
        "best_equity": START_BALANCE,
        "status": "active",
        "updated_at": now,
    }


def save_account(uid: int, balance: float, best_equity: float, status: str) -> None:
    with store.db() as c:
        c.execute(
            "UPDATE real_accounts SET balance=?, best_equity=?, status=?, updated_at=? "
            "WHERE user_id=?",
            (balance, best_equity, status, time.time(), uid),
        )


def reset_account(uid: int) -> None:
    with store.db() as c:
        c.execute("DELETE FROM real_positions WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM real_trades WHERE user_id = ?", (uid,))
        c.execute(
            "UPDATE real_accounts SET balance=?, start_balance=?, best_equity=?, "
            "status='active', updated_at=? WHERE user_id=?",
            (START_BALANCE, START_BALANCE, START_BALANCE, time.time(), uid),
        )


def load_state() -> tuple[dict[int, dict], list[dict]]:
    with store.db() as c:
        accounts = {
            r["user_id"]: dict(r)
            for r in c.execute("SELECT * FROM real_accounts")
        }
        positions = [
            dict(r) for r in c.execute("SELECT * FROM real_positions")
        ]
    return accounts, positions


# ---------- 持仓 / 成交 ----------

def insert_position(uid: int, pair: str, side: int, lots: float,
                    entry: float, leverage: float) -> int:
    with store.db() as c:
        cur = c.execute(
            "INSERT INTO real_positions"
            "(user_id, pair, side, lots, leverage, entry_price, opened_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (uid, pair, side, lots, leverage, entry, time.time()),
        )
        return int(cur.lastrowid)


def delete_position(pid: int) -> None:
    with store.db() as c:
        c.execute("DELETE FROM real_positions WHERE id = ?", (pid,))


def insert_trade(uid: int, pair: str, side: int, lots: float, entry: float,
                 exit_price: float, pnl: float, reason: str,
                 opened_at: float, closed_at: float, leverage: float) -> None:
    with store.db() as c:
        c.execute(
            "INSERT INTO real_trades"
            "(user_id, pair, side, lots, leverage, entry_price, exit_price, pnl, "
            "reason, opened_at, closed_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, pair, side, lots, leverage, entry, exit_price, pnl,
             reason, opened_at, closed_at),
        )


def trade_page(uid: int, limit: int = 10, offset: int = 0) -> list[dict]:
    with store.db() as c:
        rows = c.execute(
            "SELECT id, pair, side, lots, leverage, entry_price, exit_price, "
            "pnl, reason, opened_at, closed_at "
            "FROM real_trades WHERE user_id = ? "
            "ORDER BY closed_at DESC, id DESC LIMIT ? OFFSET ?",
            (uid, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def trade_count(uid: int) -> int:
    with store.db() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM real_trades WHERE user_id = ?", (uid,)
        ).fetchone()
    return int(row["n"]) if row else 0


def trade_stats(uid: int) -> dict:
    with store.db() as c:
        row = c.execute(
            """
            SELECT
                COUNT(*)                                                AS total,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0)   AS wins,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0)   AS losses,
                COALESCE(SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END), 0)   AS flats,
                COALESCE(SUM(pnl), 0)                                   AS net,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) AS gross_win,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0) AS gross_loss,
                COALESCE(MAX(pnl), 0)                                   AS best,
                COALESCE(MIN(pnl), 0)                                   AS worst
            FROM real_trades WHERE user_id = ?
            """,
            (uid,),
        ).fetchone()
        seq = c.execute(
            "SELECT pnl FROM real_trades WHERE user_id = ? "
            "ORDER BY closed_at ASC, id ASC",
            (uid,),
        ).fetchall()
    wins = int(row["wins"])
    losses = int(row["losses"])
    decided = wins + losses
    gross_win = float(row["gross_win"])
    gross_loss = float(row["gross_loss"])
    best_streak = worst_streak = cur_win = cur_loss = 0
    for r in seq:
        pnl = float(r["pnl"])
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
        "total": int(row["total"]),
        "wins": wins,
        "losses": losses,
        "flats": int(row["flats"]),
        "win_rate": round(wins / decided * 100.0, 1) if decided else 0.0,
        "net": round(float(row["net"])),
        "gross_win": round(gross_win),
        "gross_loss": round(gross_loss),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "best": round(float(row["best"])),
        "worst": round(float(row["worst"])),
        "best_streak": best_streak,
        "worst_streak": worst_streak,
    }
