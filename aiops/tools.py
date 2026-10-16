"""XM05 五个取证工具：白名单 + 参数校验 + 结构化错误。

设计（沿用 XM02/XM03 风格）：
- 工具只直查数据（副本 db / kb.json），不让模型编数字
- 参数非法 → 返回结构化错误（不抛未捕获异常）
- 白名单：未注册工具名 → 拒绝
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aiops.db_access import DB_PATH, _classify_failure
from knowledge.retriever import search as kb_search


def err_bad_param(msg: str) -> dict[str, Any]:
    return {"ok": False, "error": msg, "code": "bad_param"}


def err_unknown_tool(name: str) -> dict[str, Any]:
    return {"ok": False, "error": f"未注册的工具: {name}", "code": "unknown_tool"}


# ============ 工具 1：query_check_records ============
def query_check_records(target_id: int | None = None, limit: int = 20) -> dict[str, Any]:
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        return err_bad_param(f"limit 必须为非负整数，收到: {limit!r}")
    if target_id is not None and (not isinstance(target_id, int) or target_id <= 0):
        return err_bad_param(f"target_id 必须为正整数，收到: {target_id!r}")

    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        sql = (
            "SELECT cr.id, cr.target_id, mt.name, mt.url, cr.status_code, "
            "cr.duration_ms, cr.is_success, cr.error_msg, cr.checked_at "
            "FROM check_result cr JOIN monitor_target mt ON mt.id = cr.target_id WHERE 1=1"
        )
        params: list[Any] = []
        if target_id is not None:
            sql += " AND cr.target_id = ?"
            params.append(target_id)
        sql += " ORDER BY cr.id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    records = []
    for r in rows:
        rid, tid, name, url, sc, dur, succ, emsg, checked_at = r
        records.append({
            "id": rid, "target_id": tid, "target_name": name, "url": url,
            "status_code": sc, "duration_ms": dur, "is_success": bool(succ),
            "error_msg": emsg, "checked_at": str(checked_at),
            "failure_reason": _classify_failure(sc, emsg) if not succ else "",
        })
    return {"ok": True, "count": len(records), "records": records}


# ============ 工具 2：query_metrics ============
def query_metrics() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        total, succ = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_success),0) FROM check_result"
        ).fetchone()
        fail_rows = conn.execute(
            "SELECT status_code, error_msg FROM check_result WHERE is_success=0"
        ).fetchall()
        durations = [r[0] for r in conn.execute(
            "SELECT duration_ms FROM check_result WHERE is_success=1"
        ).fetchall()]
    finally:
        conn.close()

    p95 = None
    if durations:
        sd = sorted(durations)
        p95 = round(sd[max(int(0.95 * len(sd)) - 1, 0)], 1)
    reasons = Counter(_classify_failure(sc, emsg) for sc, emsg in fail_rows)
    return {
        "ok": True,
        "total_checks": total, "success_count": succ, "failure_count": total - succ,
        "success_rate": round(succ / total * 100, 1) if total else 0.0,
        "p95_duration_ms": p95, "failure_reasons": dict(reasons),
    }


# ============ 工具 3：query_logs ============
def query_logs(reason: str | None = None, limit: int = 20) -> dict[str, Any]:
    if reason is not None and reason not in ("timeout", "connect", "exception", "bad_status"):
        return err_bad_param(f"reason 必须是 timeout/connect/exception/bad_status，收到: {reason!r}")
    if not isinstance(limit, int) or limit < 0:
        return err_bad_param(f"limit 必须为非负整数，收到: {limit!r}")

    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT cr.id, mt.name, cr.status_code, cr.error_msg, cr.duration_ms, cr.checked_at "
            "FROM check_result cr JOIN monitor_target mt ON mt.id = cr.target_id "
            "WHERE cr.is_success = 0 ORDER BY cr.id DESC"
        ).fetchall()
    finally:
        conn.close()

    logs = []
    for r in rows:
        rid, name, sc, emsg, dur, checked_at = r
        rsn = _classify_failure(sc, emsg)
        if reason is not None and rsn != reason:
            continue
        logs.append({
            "check_id": rid, "target_name": name, "status_code": sc,
            "error_msg": emsg, "duration_ms": dur, "checked_at": str(checked_at),
            "reason": rsn,
        })
    return {"ok": True, "count": len(logs), "logs": logs[:limit]}



# ============ 工具 4：query_traces ============
def query_traces(check_id: int | None = None) -> dict[str, Any]:
    if check_id is not None and (not isinstance(check_id, int) or check_id <= 0):
        return err_bad_param(f"check_id 必须为正整数，收到: {check_id!r}")
    result: dict[str, Any] = {
        "ok": True, "has_trace_data": False,
        "message": "链路数据未采集（运行时 Jaeger 才产生），无存量可查。",
        "traces": [],
    }
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT mt.name, cr.duration_ms FROM check_result cr "
            "JOIN monitor_target mt ON mt.id = cr.target_id ORDER BY cr.id DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()
    result["duration_approx_ms"] = [{"target_name": n, "duration_ms": d} for n, d in rows]
    return result


# ============ 工具 5：search_knowledge ============
def search_knowledge(query: str, top_k: int = 3) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        return err_bad_param("query 必须是非空字符串")
    if not isinstance(top_k, int) or top_k <= 0:
        return err_bad_param(f"top_k 必须为正整数，收到: {top_k!r}")
    results = kb_search(query, top_k=top_k)
    return {"ok": True, "count": len(results), "results": results}


# ============ 白名单注册表 ============
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    run: Callable[..., dict[str, Any]]


_TOOLS: dict[str, Tool] = {}


def register(name: str, description: str, func: Callable[..., dict[str, Any]]) -> None:
    if name in _TOOLS:
        raise ValueError(f"工具已注册: {name}")
    _TOOLS[name] = Tool(name=name, description=description, run=func)


def list_tools() -> list[str]:
    return sorted(_TOOLS)


def describe_tools() -> str:
    return "\n".join(f"{t.name} - {t.description}" for t in _TOOLS.values())


def call_tool(name: str, **kwargs: Any) -> dict[str, Any]:
    if name not in _TOOLS:
        return err_unknown_tool(name)
    return _TOOLS[name].run(**kwargs)


register("query_check_records", "查巡检记录：哪次、什么错、耗时多少", query_check_records)
register("query_metrics", "查指标：成功率 / P95 / 失败分类计数", query_metrics)
register("query_logs", "按失败分类检索日志摘要", query_logs)
register("query_traces", "查链路（无存量数据时如实返回空）", query_traces)
register("search_knowledge", "RAG 检索运维知识库", search_knowledge)
