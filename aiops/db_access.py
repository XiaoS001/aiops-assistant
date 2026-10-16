"""XM05 数据访问：只读打开 XM01 数据副本。

审查要求（2026-10-16）：
- 数据源用副本 data/xm01_copy.db（不是 XM01 原库）
- 所有读写都在副本上进行，保证 XM01 原库哈希不变
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "xm01_copy.db"

# 失败四分类（从 XM01 metrics.py 复制而来，未改 XM01）
TIMEOUT_PREFIX = "请求超时:"
CONNECT_PREFIX = "连接失败:"
EXCEPTION_PREFIX = "请求异常:"


def _classify_failure(status_code: int | None, error_msg: str | None) -> str:
    """失败四分类：timeout / connect / exception / bad_status。

    与 XM01 metrics.py 的 _classify_failure 逻辑一致（复制，不改 XM01）。
    """
    if error_msg is not None and error_msg.startswith(TIMEOUT_PREFIX):
        return "timeout"
    if error_msg is not None and error_msg.startswith(CONNECT_PREFIX):
        return "connect"
    if error_msg is not None and error_msg.startswith(EXCEPTION_PREFIX):
        return "exception"
    if status_code is not None:
        return "bad_status"
    return "exception"


@dataclass(frozen=True)
class CheckRecord:
    """一条巡检记录（从副本读出）。"""
    id: int
    target_id: int
    target_name: str
    url: str
    status_code: int | None
    duration_ms: float
    is_success: bool
    error_msg: str | None
    checked_at: str
    failure_reason: str  # 失败分类；成功则为 ""


def _open_ro() -> sqlite3.Connection:
    """只读打开副本（uri mode=ro 防写）。"""
    return sqlite3.connect(
        f"file:{DB_PATH.as_posix()}?mode=ro", uri=True
    )


def list_failures(limit: int = 50) -> list[CheckRecord]:
    """列出巡检失败记录（带失败分类）。"""
    conn = _open_ro()
    try:
        cur = conn.execute(
            """
            SELECT cr.id, cr.target_id, mt.name, mt.url,
                   cr.status_code, cr.duration_ms, cr.is_success,
                   cr.error_msg, cr.checked_at
            FROM check_result cr
            JOIN monitor_target mt ON mt.id = cr.target_id
            WHERE cr.is_success = 0
            ORDER BY cr.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    records = []
    for row in rows:
        (rid, tid, name, url, sc, dur, succ, emsg, checked_at) = row
        reason = _classify_failure(sc, emsg) if not succ else ""
        records.append(CheckRecord(
            id=rid, target_id=tid, target_name=name, url=url,
            status_code=sc, duration_ms=dur, is_success=bool(succ),
            error_msg=emsg, checked_at=str(checked_at), failure_reason=reason,
        ))
    return records


def summary() -> dict[str, Any]:
    """巡检总览：总数 / 成功 / 失败 / 成功率 / 失败分类计数。"""
    conn = _open_ro()
    try:
        total, succ = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_success),0) FROM check_result"
        ).fetchone()
        fail = total - succ
        rows = conn.execute(
            "SELECT status_code, error_msg FROM check_result WHERE is_success=0"
        ).fetchall()
    finally:
        conn.close()

    from collections import Counter
    reasons = Counter(_classify_failure(sc, emsg) for sc, emsg in rows)
    return {
        "total": total,
        "success": succ,
        "fail": fail,
        "success_rate": round(succ / total * 100, 1) if total else 0.0,
        "failure_reasons": dict(reasons),
    }


def list_targets() -> list[dict[str, Any]]:
    """列出监控目标。"""
    conn = _open_ro()
    try:
        rows = conn.execute(
            "SELECT id, name, url, timeout, enabled FROM monitor_target"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "name": r[1], "url": r[2], "timeout": r[3], "enabled": r[4]}
        for r in rows
    ]
