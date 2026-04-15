#!/usr/bin/env python3
"""
db.py — SQLite 데이터베이스 관리자

모든 거래, 스크리닝, 데몬 사이클, 에러 이력을 SQLite에 저장합니다.

DB 위치: ~/Library/Application Support/tossctl/trading.db

테이블:
  trades           — 거래 이력
  screener_runs    — 스크리닝 실행 이력
  screener_results — 스크리닝 종목별 결과
  daemon_cycles    — 데몬 사이클 이력
  errors           — 에러 로그

사용법 (CLI):
  python3 db.py migrate          # trade-log.json → DB 마이그레이션
  python3 db.py trades [--date YYYY-MM-DD] [--symbol SYM] [--status open|closed]
  python3 db.py screener [--date YYYY-MM-DD] [--symbol SYM]
  python3 db.py cycles [--date YYYY-MM-DD] [--limit 20]
  python3 db.py errors [--limit 50]
  python3 db.py stats [--date YYYY-MM-DD]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path.home() / "Library/Application Support/tossctl/trading.db"
TRADE_LOG_JSON = Path.home() / "Library/Application Support/tossctl/trade-log.json"


# ─── 스키마 ───

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    name            TEXT    DEFAULT '',
    side            TEXT    DEFAULT 'buy',
    market          TEXT    DEFAULT 'US',
    quantity        REAL    DEFAULT 0,
    entry_price     REAL    DEFAULT 0,
    entry_date      TEXT,
    entry_time      TEXT,
    entry_grade     TEXT    DEFAULT '',
    entry_score     REAL    DEFAULT 0,
    entry_reason    TEXT    DEFAULT '',
    stop_loss       REAL    DEFAULT 0,
    take_profit     REAL    DEFAULT 0,
    exit_price      REAL,
    exit_date       TEXT,
    exit_time       TEXT,
    exit_reason     TEXT,
    pnl_pct         REAL,
    pnl_amount      REAL,
    lesson          TEXT,
    lesson_score    INTEGER,
    status          TEXT    DEFAULT 'open',
    mode            TEXT    DEFAULT 'manual',
    tags            TEXT    DEFAULT '[]',
    group_id        TEXT,
    tranche_seq     INTEGER DEFAULT 1,
    created_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS tranche_plans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id          TEXT    UNIQUE NOT NULL,
    symbol            TEXT    NOT NULL,
    market            TEXT    DEFAULT 'US',
    total_tranches    INTEGER DEFAULT 3,
    planned_qty       INTEGER DEFAULT 0,
    planned_budget    REAL    DEFAULT 0,
    entry_price_t1    REAL    DEFAULT 0,
    ratios            TEXT    DEFAULT '[0.4, 0.35, 0.25]',
    tranches_filled   INTEGER DEFAULT 1,
    next_tranche      INTEGER DEFAULT 2,
    next_condition    TEXT    DEFAULT 'dip_buy',
    next_target_price REAL    DEFAULT 0,
    cycles_waited     INTEGER DEFAULT 0,
    max_wait_cycles   INTEGER DEFAULT 12,
    status            TEXT    DEFAULT 'active',
    grade             TEXT    DEFAULT '',
    score             REAL    DEFAULT 0,
    entry_reason      TEXT    DEFAULT '',
    created_at        TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at        TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS screener_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT    NOT NULL,
    source          TEXT    DEFAULT 'all',
    market          TEXT    DEFAULT 'all',
    total_scanned   INTEGER DEFAULT 0,
    total_scored    INTEGER DEFAULT 0,
    buy_count       INTEGER DEFAULT 0,
    watch_count     INTEGER DEFAULT 0,
    skip_count      INTEGER DEFAULT 0,
    created_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS screener_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES screener_runs(id),
    symbol          TEXT,
    name            TEXT    DEFAULT '',
    grade           TEXT,
    score           REAL    DEFAULT 0,
    source          TEXT    DEFAULT '',
    vol_spike       REAL,
    gap_pct         REAL,
    current_price   REAL    DEFAULT 0,
    recommendation  TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS daemon_cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_num       INTEGER,
    market          TEXT    DEFAULT '',
    status          TEXT    DEFAULT 'ok',
    positions_count INTEGER DEFAULT 0,
    sells_executed  INTEGER DEFAULT 0,
    buys_attempted  INTEGER DEFAULT 0,
    buys_filled     INTEGER DEFAULT 0,
    today_pnl       REAL    DEFAULT 0,
    today_trades    INTEGER DEFAULT 0,
    consecutive_losses INTEGER DEFAULT 0,
    duration_sec    REAL    DEFAULT 0,
    note            TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    DEFAULT 'daemon',
    message         TEXT    NOT NULL,
    detail          TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_date      ON trades(entry_date);
CREATE INDEX IF NOT EXISTS idx_trades_status    ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_group     ON trades(group_id);
CREATE INDEX IF NOT EXISTS idx_tranche_plans_symbol ON tranche_plans(symbol);
CREATE INDEX IF NOT EXISTS idx_tranche_plans_status ON tranche_plans(status);
CREATE INDEX IF NOT EXISTS idx_screener_run     ON screener_results(run_id);
CREATE INDEX IF NOT EXISTS idx_screener_symbol  ON screener_results(symbol);
CREATE INDEX IF NOT EXISTS idx_cycles_date      ON daemon_cycles(created_at);
CREATE INDEX IF NOT EXISTS idx_errors_date      ON errors(created_at);
"""


# ─── 연결 관리 ───

@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """테이블 생성 (없을 때만) + 스키마 마이그레이션"""
    with get_conn() as conn:
        # 기존 테이블에 신규 컬럼 추가 (SCHEMA 실행 전에 해야 인덱스 생성 성공)
        _migrate_schema(conn)
        conn.executescript(SCHEMA)


def _migrate_schema(conn):
    """기존 테이블에 신규 컬럼 추가 (없을 때만)"""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "trades" not in tables:
        return  # 첫 실행 — SCHEMA가 모든 것을 생성함
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "group_id" not in existing:
        conn.execute("ALTER TABLE trades ADD COLUMN group_id TEXT")
    if "tranche_seq" not in existing:
        conn.execute("ALTER TABLE trades ADD COLUMN tranche_seq INTEGER DEFAULT 1")


# ─── 거래 기록 ───

def insert_trade(t: dict) -> int:
    with get_conn() as conn:
        tags = t.get("tags", [])
        if isinstance(tags, list):
            tags = json.dumps(tags, ensure_ascii=False)
        cur = conn.execute("""
            INSERT INTO trades
              (symbol, name, side, market, quantity,
               entry_price, entry_date, entry_time,
               entry_grade, entry_score, entry_reason,
               stop_loss, take_profit, status, mode, tags,
               exit_price, exit_date, exit_time, exit_reason,
               pnl_pct, pnl_amount, lesson, lesson_score,
               group_id, tranche_seq)
            VALUES
              (:symbol,:name,:side,:market,:quantity,
               :entry_price,:entry_date,:entry_time,
               :entry_grade,:entry_score,:entry_reason,
               :stop_loss,:take_profit,:status,:mode,:tags,
               :exit_price,:exit_date,:exit_time,:exit_reason,
               :pnl_pct,:pnl_amount,:lesson,:lesson_score,
               :group_id,:tranche_seq)
        """, {
            "symbol":       t.get("symbol", ""),
            "name":         t.get("name", ""),
            "side":         t.get("side", "buy"),
            "market":       t.get("market", "US"),
            "quantity":     t.get("quantity", 0),
            "entry_price":  t.get("entry_price", 0),
            "entry_date":   t.get("entry_date"),
            "entry_time":   t.get("entry_time"),
            "entry_grade":  t.get("entry_grade", ""),
            "entry_score":  t.get("entry_score", 0),
            "entry_reason": t.get("entry_reason", ""),
            "stop_loss":    t.get("stop_loss", 0),
            "take_profit":  t.get("take_profit", 0),
            "status":       t.get("status", "open"),
            "mode":         t.get("mode", "manual"),
            "tags":         tags,
            "exit_price":   t.get("exit_price"),
            "exit_date":    t.get("exit_date"),
            "exit_time":    t.get("exit_time"),
            "exit_reason":  t.get("exit_reason"),
            "pnl_pct":      t.get("pnl_pct"),
            "pnl_amount":   t.get("pnl_amount"),
            "lesson":       t.get("lesson"),
            "lesson_score": t.get("lesson_score"),
            "group_id":     t.get("group_id"),
            "tranche_seq":  t.get("tranche_seq", 1),
        })
        return cur.lastrowid


def close_trade_db(symbol: str, exit_price: float, exit_reason: str,
                   exit_time: str = None, pnl_pct: float = None,
                   pnl_amount: float = None) -> dict | None:
    now = datetime.now()
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM trades
            WHERE symbol = ? AND status = 'open'
            ORDER BY id DESC LIMIT 1
        """, (symbol,)).fetchone()
        if not row:
            return None

        entry = row["entry_price"]
        if pnl_pct is None and entry > 0:
            cost_rate = 0.002
            gross = (exit_price - entry) / entry
            pnl_pct = round((gross - cost_rate) * 100, 2)
        if pnl_amount is None:
            pnl_amount = round((exit_price - entry) * row["quantity"], 2)

        tags = json.loads(row["tags"] or "[]")
        tags.append("win" if (pnl_pct or 0) > 0 else "loss")
        if exit_reason == "SELL_STOP_LOSS":
            tags.append("stopped_out")
        if exit_reason == "SELL_TAKE_PROFIT":
            tags.append("target_hit")

        conn.execute("""
            UPDATE trades SET
              exit_price=?, exit_date=?, exit_time=?, exit_reason=?,
              pnl_pct=?, pnl_amount=?, status='closed', tags=?
            WHERE id=?
        """, (
            exit_price,
            now.strftime("%Y-%m-%d"),
            exit_time or now.strftime("%H:%M:%S"),
            exit_reason,
            pnl_pct,
            pnl_amount,
            json.dumps(tags, ensure_ascii=False),
            row["id"],
        ))
        return dict(row) | {"exit_price": exit_price, "pnl_pct": pnl_pct,
                             "status": "closed", "exit_reason": exit_reason}


def update_lesson_db(symbol: str, lesson: str, score: int) -> bool:
    with get_conn() as conn:
        r = conn.execute("""
            UPDATE trades SET lesson=?, lesson_score=?
            WHERE id = (
                SELECT id FROM trades
                WHERE symbol=? AND status='closed'
                ORDER BY id DESC LIMIT 1
            )
        """, (lesson, score, symbol))
        return r.rowcount > 0


def query_trades(symbol: str = None, date_str: str = None,
                 status: str = "all", limit: int = 100) -> list:
    where, params = [], []
    if symbol:
        where.append("symbol = ?"); params.append(symbol.upper())
    if date_str:
        where.append("(entry_date = ? OR exit_date = ?)"); params += [date_str, date_str]
    if status != "all":
        where.append("status = ?"); params.append(status)
    sql = "SELECT * FROM trades"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY id DESC LIMIT {int(limit)}"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ─── 분할매수 트랜치 ───

def create_tranche_plan(plan: dict) -> str:
    """트랜치 플랜 생성. group_id 반환."""
    group_id = plan.get("group_id") or f"{plan['symbol']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ratios = plan.get("ratios", [0.4, 0.35, 0.25])
    if isinstance(ratios, list):
        ratios = json.dumps(ratios)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO tranche_plans
              (group_id, symbol, market, total_tranches, planned_qty,
               planned_budget, entry_price_t1, ratios, tranches_filled,
               next_tranche, next_condition, next_target_price,
               cycles_waited, max_wait_cycles, status, grade, score,
               entry_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            group_id,
            plan.get("symbol", ""),
            plan.get("market", "US"),
            plan.get("total_tranches", 3),
            plan.get("planned_qty", 0),
            plan.get("planned_budget", 0),
            plan.get("entry_price_t1", 0),
            ratios,
            plan.get("tranches_filled", 1),
            plan.get("next_tranche", 2),
            plan.get("next_condition", "dip_buy"),
            plan.get("next_target_price", 0),
            plan.get("cycles_waited", 0),
            plan.get("max_wait_cycles", 12),
            "active",
            plan.get("grade", ""),
            plan.get("score", 0),
            plan.get("entry_reason", ""),
        ))
    return group_id


def get_active_tranche_plans(symbol: str = None) -> list:
    """활성 트랜치 플랜 목록 조회."""
    with get_conn() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM tranche_plans WHERE status='active' AND symbol=? ORDER BY id",
                (symbol.upper(),)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tranche_plans WHERE status='active' ORDER BY id"
            ).fetchall()
    return [dict(r) for r in rows]


def update_tranche_plan(group_id: str, updates: dict) -> bool:
    """트랜치 플랜 업데이트 (cycles_waited 증가, next_tranche 변경 등)."""
    if not updates:
        return False
    sets = []
    params = []
    for k, v in updates.items():
        sets.append(f"{k}=?")
        params.append(v)
    sets.append("updated_at=datetime('now','localtime')")
    params.append(group_id)
    with get_conn() as conn:
        r = conn.execute(
            f"UPDATE tranche_plans SET {','.join(sets)} WHERE group_id=?",
            params
        )
        return r.rowcount > 0


def complete_tranche_plan(group_id: str, status: str = "completed") -> bool:
    """트랜치 플랜 종료 (completed, expired, cancelled)."""
    with get_conn() as conn:
        r = conn.execute(
            "UPDATE tranche_plans SET status=?, updated_at=datetime('now','localtime') WHERE group_id=?",
            (status, group_id)
        )
        return r.rowcount > 0


def complete_tranche_plan_by_symbol(symbol: str) -> int:
    """해당 종목의 활성 트랜치 플랜 모두 종료."""
    with get_conn() as conn:
        r = conn.execute(
            "UPDATE tranche_plans SET status='completed', updated_at=datetime('now','localtime') WHERE symbol=? AND status='active'",
            (symbol.upper(),)
        )
        return r.rowcount


def close_all_trades_by_symbol(symbol: str, exit_price: float, exit_reason: str,
                                exit_time: str = None) -> list:
    """해당 종목의 모든 open 트레이드를 일괄 종료. 종료된 레코드 목록 반환."""
    now = datetime.now()
    closed = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE symbol=? AND status='open' ORDER BY id",
            (symbol.upper(),)
        ).fetchall()
        if not rows:
            return []

        for row in rows:
            entry = row["entry_price"]
            pnl_pct = None
            pnl_amount = None
            if entry > 0:
                cost_rate = 0.002
                gross = (exit_price - entry) / entry
                pnl_pct = round((gross - cost_rate) * 100, 2)
            if entry > 0:
                pnl_amount = round((exit_price - entry) * row["quantity"], 2)

            tags = json.loads(row["tags"] or "[]")
            tags.append("win" if (pnl_pct or 0) > 0 else "loss")
            if exit_reason == "SELL_STOP_LOSS":
                tags.append("stopped_out")
            if exit_reason == "SELL_TAKE_PROFIT":
                tags.append("target_hit")

            conn.execute("""
                UPDATE trades SET
                  exit_price=?, exit_date=?, exit_time=?, exit_reason=?,
                  pnl_pct=?, pnl_amount=?, status='closed', tags=?
                WHERE id=?
            """, (
                exit_price,
                now.strftime("%Y-%m-%d"),
                exit_time or now.strftime("%H:%M:%S"),
                exit_reason,
                pnl_pct,
                pnl_amount,
                json.dumps(tags, ensure_ascii=False),
                row["id"],
            ))
            closed.append(dict(row) | {
                "exit_price": exit_price, "pnl_pct": pnl_pct,
                "status": "closed", "exit_reason": exit_reason,
            })
    return closed


# ─── 스크리너 기록 ───

def insert_screener_run(data: dict) -> int:
    """scan() 결과 전체를 DB에 저장. run_id 반환."""
    summary = data.get("summary", {})
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO screener_runs
              (run_at, source, market, total_scanned, total_scored,
               buy_count, watch_count, skip_count)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            data.get("timestamp", datetime.now().isoformat()),
            ",".join(data.get("sources", [])),
            "all",
            data.get("total_scanned", 0),
            data.get("total_scored", 0),
            summary.get("buy", 0),
            summary.get("watch", 0),
            summary.get("skip", 0),
        ))
        run_id = cur.lastrowid

        all_results = (
            [(c, "buy")   for c in data.get("buy_candidates", [])] +
            [(c, "watch") for c in data.get("watch_list", [])] +
            [(c, "skip")  for c in data.get("skip_list", [])]
        )
        for c, bucket in all_results:
            conn.execute("""
                INSERT INTO screener_results
                  (run_id, symbol, name, grade, score, source,
                   vol_spike, gap_pct, current_price, recommendation)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                run_id,
                c.get("symbol", ""),
                c.get("name", ""),
                c.get("grade", bucket[0].upper()),
                c.get("score_pct", c.get("score", 0)),
                c.get("_source", ""),
                c.get("_vol_spike"),
                c.get("_gap_pct"),
                c.get("current_price", 0),
                c.get("recommendation", ""),
            ))
        return run_id


def query_screener(symbol: str = None, date_str: str = None,
                   grade: str = None, limit: int = 50) -> list:
    where, params = ["1=1"], []
    if symbol:
        where.append("r.symbol = ?"); params.append(symbol.upper())
    if date_str:
        where.append("date(s.run_at) = ?"); params.append(date_str)
    if grade:
        where.append("r.grade = ?"); params.append(grade.upper())
    sql = f"""
        SELECT r.symbol, r.name, r.grade, r.score, r.current_price,
               r.source, r.vol_spike, r.gap_pct, r.recommendation,
               s.run_at, date(s.run_at) as run_date
        FROM screener_results r
        JOIN screener_runs s ON r.run_id = s.id
        WHERE {' AND '.join(where)}
        ORDER BY s.id DESC, r.score DESC
        LIMIT {int(limit)}
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ─── 데몬 사이클 기록 ───

def insert_cycle(cycle: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO daemon_cycles
              (cycle_num, market, status, positions_count,
               sells_executed, buys_attempted, buys_filled,
               today_pnl, today_trades, consecutive_losses,
               duration_sec, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            cycle.get("cycle_num", 0),
            cycle.get("market", ""),
            cycle.get("status", "ok"),
            cycle.get("positions_count", 0),
            cycle.get("sells_executed", 0),
            cycle.get("buys_attempted", 0),
            cycle.get("buys_filled", 0),
            cycle.get("today_pnl", 0),
            cycle.get("today_trades", 0),
            cycle.get("consecutive_losses", 0),
            cycle.get("duration_sec", 0),
            cycle.get("note", ""),
        ))
        return cur.lastrowid


def query_cycles(date_str: str = None, limit: int = 50) -> list:
    where, params = [], []
    if date_str:
        where.append("date(created_at) = ?"); params.append(date_str)
    sql = "SELECT * FROM daemon_cycles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY id DESC LIMIT {int(limit)}"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ─── 에러 기록 ───

def insert_error(source: str, message: str, detail: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO errors (source, message, detail) VALUES (?,?,?)",
            (source, message, detail)
        )
        return cur.lastrowid


def query_errors(date_str: str = None, limit: int = 50) -> list:
    where, params = [], []
    if date_str:
        where.append("date(created_at) = ?"); params.append(date_str)
    sql = "SELECT * FROM errors"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY id DESC LIMIT {int(limit)}"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ─── 통계 ───

def daily_stats(date_str: str = None) -> dict:
    d = date_str or date.today().isoformat()
    with get_conn() as conn:
        tr = conn.execute("""
            SELECT
              COUNT(*) as total,
              SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as closed,
              SUM(CASE WHEN status='open'   THEN 1 ELSE 0 END) as open_cnt,
              SUM(CASE WHEN pnl_pct > 0 AND status='closed' THEN 1 ELSE 0 END) as wins,
              ROUND(AVG(CASE WHEN status='closed' THEN pnl_pct END), 2) as avg_pnl,
              ROUND(SUM(CASE WHEN status='closed' THEN pnl_pct ELSE 0 END), 2) as total_pnl
            FROM trades
            WHERE entry_date = ? OR exit_date = ?
        """, (d, d)).fetchone()

        sc = conn.execute("""
            SELECT COUNT(*) as runs,
                   SUM(total_scanned) as scanned,
                   SUM(buy_count) as buy_signals
            FROM screener_runs
            WHERE date(run_at) = ?
        """, (d,)).fetchone()

        cy = conn.execute("""
            SELECT COUNT(*) as cycles,
                   MAX(cycle_num) as last_cycle
            FROM daemon_cycles
            WHERE date(created_at) = ?
        """, (d,)).fetchone()

        er = conn.execute("""
            SELECT COUNT(*) as cnt FROM errors WHERE date(created_at) = ?
        """, (d,)).fetchone()

    wins = tr["wins"] or 0
    closed = tr["closed"] or 0
    return {
        "date": d,
        "trades": {
            "total": tr["total"] or 0,
            "open": tr["open_cnt"] or 0,
            "closed": closed,
            "wins": wins,
            "losses": closed - wins,
            "win_rate": round(wins / closed * 100, 1) if closed > 0 else 0,
            "avg_pnl": tr["avg_pnl"] or 0,
            "total_pnl": tr["total_pnl"] or 0,
        },
        "screener": {
            "runs": sc["runs"] or 0,
            "scanned": sc["scanned"] or 0,
            "buy_signals": sc["buy_signals"] or 0,
        },
        "daemon": {
            "cycles": cy["cycles"] or 0,
            "last_cycle": cy["last_cycle"] or 0,
        },
        "errors": er["cnt"] or 0,
    }


# ─── JSON 마이그레이션 ───

def migrate_from_json() -> dict:
    """기존 trade-log.json을 DB로 마이그레이션"""
    if not TRADE_LOG_JSON.exists():
        return {"migrated": 0, "skipped": 0}

    trades = json.loads(TRADE_LOG_JSON.read_text())
    migrated = 0
    skipped = 0

    with get_conn() as conn:
        for t in trades:
            # 이미 있는지 확인 (symbol + entry_date + entry_time)
            existing = conn.execute("""
                SELECT id FROM trades
                WHERE symbol=? AND entry_date=? AND entry_time=?
            """, (t.get("symbol",""), t.get("entry_date",""), t.get("entry_time",""))).fetchone()
            if existing:
                skipped += 1
                continue

            tags = t.get("tags", [])
            if isinstance(tags, list):
                tags = json.dumps(tags, ensure_ascii=False)
            conn.execute("""
                INSERT INTO trades
                  (symbol, name, side, market, quantity,
                   entry_price, entry_date, entry_time,
                   entry_grade, entry_score, entry_reason,
                   stop_loss, take_profit, status, mode, tags,
                   exit_price, exit_date, exit_reason,
                   pnl_pct, pnl_amount, lesson, lesson_score)
                VALUES
                  (:symbol,:name,:side,:market,:quantity,
                   :entry_price,:entry_date,:entry_time,
                   :entry_grade,:entry_score,:entry_reason,
                   :stop_loss,:take_profit,:status,:mode,:tags,
                   :exit_price,:exit_date,:exit_reason,
                   :pnl_pct,:pnl_amount,:lesson,:lesson_score)
            """, {
                "symbol":       t.get("symbol", ""),
                "name":         t.get("name", ""),
                "side":         t.get("side", "buy"),
                "market":       t.get("market", "US"),
                "quantity":     t.get("quantity", 0),
                "entry_price":  t.get("entry_price", 0),
                "entry_date":   t.get("entry_date"),
                "entry_time":   t.get("entry_time"),
                "entry_grade":  t.get("entry_grade", ""),
                "entry_score":  t.get("entry_score", 0),
                "entry_reason": t.get("entry_reason", ""),
                "stop_loss":    t.get("stop_loss", 0),
                "take_profit":  t.get("take_profit", 0),
                "status":       t.get("status", "open"),
                "mode":         t.get("mode", "manual"),
                "tags":         tags,
                "exit_price":   t.get("exit_price"),
                "exit_date":    t.get("exit_date"),
                "exit_reason":  t.get("exit_reason"),
                "pnl_pct":      t.get("pnl_pct"),
                "pnl_amount":   t.get("pnl_amount"),
                "lesson":       t.get("lesson"),
                "lesson_score": t.get("lesson_score"),
            })
            migrated += 1

    return {"migrated": migrated, "skipped": skipped}


# ─── CLI ───

def main():
    init_db()

    if len(sys.argv) < 2:
        print(json.dumps({"error": "command: migrate | trades | screener | cycles | errors | stats"}))
        sys.exit(1)

    cmd = sys.argv[1]
    args = {}
    i = 2
    while i < len(sys.argv):
        if sys.argv[i].startswith("--") and i + 1 < len(sys.argv):
            args[sys.argv[i][2:]] = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    if cmd == "migrate":
        result = migrate_from_json()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "trades":
        rows = query_trades(
            symbol=args.get("symbol"),
            date_str=args.get("date"),
            status=args.get("status", "all"),
            limit=int(args.get("limit", 100)),
        )
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    elif cmd == "screener":
        rows = query_screener(
            symbol=args.get("symbol"),
            date_str=args.get("date"),
            grade=args.get("grade"),
            limit=int(args.get("limit", 50)),
        )
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    elif cmd == "cycles":
        rows = query_cycles(
            date_str=args.get("date"),
            limit=int(args.get("limit", 50)),
        )
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    elif cmd == "errors":
        rows = query_errors(
            date_str=args.get("date"),
            limit=int(args.get("limit", 50)),
        )
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    elif cmd == "stats":
        result = daily_stats(args.get("date"))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        print(json.dumps({"error": f"unknown command: {cmd}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
