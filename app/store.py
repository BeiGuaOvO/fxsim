from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.abspath(os.getenv("FX_DB", os.path.join(BASE_DIR, "data", "fxsim.db")))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
START_BALANCE = float(os.getenv("FX_START_BALANCE", "300000"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance REAL NOT NULL,
    start_balance REAL NOT NULL,
    best_equity REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    pair TEXT NOT NULL,
    side INTEGER NOT NULL,
    lots REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 100,
    entry_price REAL NOT NULL,
    sl REAL,
    tp REAL,
    opened_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    pair TEXT NOT NULL,
    side INTEGER NOT NULL,
    lots REAL NOT NULL,
    kind TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    sl REAL,
    tp REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    pair TEXT NOT NULL,
    side INTEGER NOT NULL,
    lots REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL,
    reason TEXT NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS equity (
    user_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    equity REAL NOT NULL,
    PRIMARY KEY (user_id, ts)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id, closed_at DESC);
"""


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


@contextmanager
def db():
    c = conn()
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db() -> None:
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(positions)")}
        if "leverage" not in cols:
            c.execute("ALTER TABLE positions ADD COLUMN leverage REAL NOT NULL DEFAULT 100")
        cols = {r["name"] for r in c.execute("PRAGMA table_info(trades)")}
        if "leverage" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN leverage REAL NOT NULL DEFAULT 100")


# ---------- 密码 ----------

def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        dk = hashlib.scrypt(
            pw.encode(),
            salt=bytes.fromhex(salt),
            n=int(n), r=int(r), p=int(p),
            dklen=len(bytes.fromhex(digest)),
        )
        return secrets.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


# ---------- 用户 ----------

def create_user(username: str, password: str) -> int:
    name = username.strip()
    if not (2 <= len(name) <= 24):
        raise ValueError("用户名长度需为 2-24 个字符")
    with db() as c:
        try:
            cur = c.execute(
                "INSERT INTO users(username, pw_hash, created_at) VALUES(?, ?, ?)",
                (name, hash_pw(password), time.time()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("用户名已存在")
        uid = int(cur.lastrowid)
        c.execute(
            "INSERT INTO accounts(user_id, balance, start_balance, best_equity, status, updated_at) "
            "VALUES(?, ?, ?, ?, 'active', ?)",
            (uid, START_BALANCE, START_BALANCE, START_BALANCE, time.time()),
        )
    return uid


def auth_user(username: str, password: str) -> Optional[int]:
    with db() as c:
        row = c.execute(
            "SELECT id, pw_hash FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
    if row and verify_pw(password, row["pw_hash"]):
        return int(row["id"])
    return None


def issue_token(user_id: int) -> str:
    tok = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        c.execute(
            "INSERT INTO sessions(token, user_id, expires_at) VALUES(?, ?, ?)",
            (tok, user_id, time.time() + 30 * 86400),
        )
    return tok


def user_id_by_token(token: str) -> Optional[int]:
    if not token:
        return None
    with db() as c:
        row = c.execute(
            "SELECT user_id FROM sessions WHERE token = ? AND expires_at > ?",
            (token, time.time()),
        ).fetchone()
    return int(row["user_id"]) if row else None


# ---------- 账户 ----------

def get_account(uid: int) -> Optional[dict]:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE user_id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def save_account(uid: int, balance: float, best_equity: float, status: str) -> None:
    with db() as c:
        c.execute(
            "UPDATE accounts SET balance=?, best_equity=?, status=?, updated_at=? WHERE user_id=?",
            (balance, best_equity, status, time.time(), uid),
        )

def save_deposit(uid: int, balance: float, start_balance: float) -> None:
    with db() as c:
        c.execute(
            "UPDATE accounts SET balance=?, start_balance=?, updated_at=? WHERE user_id=?",
            (balance, start_balance, time.time(), uid),
        )


def reset_account(uid: int) -> None:
    with db() as c:
        c.execute("DELETE FROM positions WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM orders WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM trades WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM equity WHERE user_id = ?", (uid,))
        c.execute(
            "UPDATE accounts SET balance=?, start_balance=?, best_equity=?, status='active', updated_at=? "
            "WHERE user_id=?",
            (START_BALANCE, START_BALANCE, START_BALANCE, time.time(), uid),
        )


def load_state():
    with db() as c:
        accounts = {r["user_id"]: dict(r) for r in c.execute("SELECT * FROM accounts")}
        positions = [dict(r) for r in c.execute("SELECT * FROM positions")]
        orders = [dict(r) for r in c.execute("SELECT * FROM orders")]
    return accounts, positions, orders


# ---------- 持仓 / 挂单 / 成交 ----------

def insert_position(uid: int, pair: str, side: int, lots: float,
                    entry: float, sl, tp, leverage: float = 100.0) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO positions(user_id, pair, side, lots, entry_price, sl, tp, leverage, opened_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, pair, side, lots, entry, sl, tp, leverage, time.time()),
        )
        return int(cur.lastrowid)


def delete_position(pid: int) -> None:
    with db() as c:
        c.execute("DELETE FROM positions WHERE id = ?", (pid,))


def insert_order(uid: int, pair: str, side: int, lots: float, kind: str,
                 trigger: float, sl, tp) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO orders(user_id, pair, side, lots, kind, trigger_price, sl, tp, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, pair, side, lots, kind, trigger, sl, tp, time.time()),
        )
        return int(cur.lastrowid)


def delete_order(oid: int) -> None:
    with db() as c:
        c.execute("DELETE FROM orders WHERE id = ?", (oid,))


def insert_trade(uid: int, pair: str, side: int, lots: float, entry: float,
                 exit_price: float, pnl: float, reason: str,
                 opened_at: float, closed_at: float,
                 leverage: float = 100.0) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO trades(user_id, pair, side, lots, leverage, entry_price, "
            "exit_price, pnl, reason, opened_at, closed_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, pair, side, lots, leverage, entry, exit_price, pnl,
             reason, opened_at, closed_at),
        )


def recent_trades(uid: int, limit: int = 15) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT pair, side, lots, entry_price, exit_price, pnl, reason, closed_at "
            "FROM trades WHERE user_id=? ORDER BY closed_at DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [dict(r) for r in rows]

def trade_count(uid: int) -> int:
    with db() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE user_id = ?", (uid,)
        ).fetchone()
    return int(row["n"]) if row else 0
def trade_page(uid: int, limit: int = 10, offset: int = 0) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT id, pair, side, lots, leverage, entry_price, exit_price, "
            "pnl, reason, opened_at, closed_at "
            "FROM trades WHERE user_id = ? "
            "ORDER BY closed_at DESC, id DESC LIMIT ? OFFSET ?",
            (uid, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]
def trade_stats(uid: int) -> dict:
    with db() as c:
        row = c.execute(
            """
            SELECT
                COUNT(*)                                               AS total,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0)  AS wins,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0)  AS losses,
                COALESCE(SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END), 0)  AS flats,
                COALESCE(SUM(pnl), 0)                                  AS net,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) AS gross_win,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0) AS gross_loss,
                COALESCE(MAX(pnl), 0)                                  AS best,
                COALESCE(MIN(pnl), 0)                                  AS worst
            FROM trades WHERE user_id = ?
            """,
            (uid,),
        ).fetchone()
        seq = c.execute(
            "SELECT pnl FROM trades WHERE user_id = ? ORDER BY closed_at ASC, id ASC",
            (uid,),
        ).fetchall()
    wins = int(row["wins"])
    losses = int(row["losses"])
    decided = wins + losses
    gross_win = float(row["gross_win"])
    gross_loss = float(row["gross_loss"])
    best_streak = 0
    worst_streak = 0
    cur_win = 0
    cur_loss = 0
    for r in seq:
        pnl = float(r["pnl"])
        if pnl > 0:
            cur_win += 1
            cur_loss = 0
        elif pnl < 0:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = 0
            cur_loss = 0
        if cur_win > best_streak:
            best_streak = cur_win
        if cur_loss > worst_streak:
            worst_streak = cur_loss
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


# ---------- 净值曲线 / 元数据 ----------

def snapshot_equity(uid: int, ts: float, equity: float) -> None:
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO equity(user_id, ts, equity) VALUES(?, ?, ?)",
            (uid, ts, equity),
        )


def prune_equity(uid: int, keep: int = 1500) -> None:
    with db() as c:
        c.execute(
            "DELETE FROM equity WHERE user_id=? AND ts < "
            "(SELECT ts FROM equity WHERE user_id=? ORDER BY ts DESC LIMIT 1 OFFSET ?)",
            (uid, uid, keep),
        )


def save_meta(key: str, value: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def load_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    with db() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def usernames() -> dict[int, str]:
    with db() as c:
        return {r["id"]: r["username"] for r in c.execute("SELECT id, username FROM users")}
def user_id_by_name(name: str) -> Optional[int]:
    with db() as c:
        row = c.execute("SELECT id FROM users WHERE username = ?", (name.strip(),)).fetchone()
    return int(row["id"]) if row else None
def user_info(uid: int) -> Optional[dict]:
    with db() as c:
        row = c.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?", (uid,)
        ).fetchone()
    return dict(row) if row else None
def equity_series(uid: int, limit: int = 120) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT ts, equity FROM equity WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]