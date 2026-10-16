"""XM05 AIOps 智能运维助手 —— Flask 入口。

页面：告警列表（从副本 db 失败记录生成）→ 点一条 → 看 AI 根因分析。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保能 import aiops / knowledge
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify, render_template, request

from aiops.db_access import list_failures, summary
from aiops.graph import analyze_incident

app = Flask(__name__)


@app.route("/")
def index():
    """首页：告警列表 + KPI 总览。"""
    failures = list_failures(limit=50)
    stats = summary()
    return render_template("index.html", failures=failures, stats=stats)


@app.route("/api/analyze/<int:record_id>")
def api_analyze(record_id: int):
    """分析一条巡检异常，返回 AI 根因 + 处置建议。

    两层降级：模型失败 → 规则路径；任何异常 → 结构化结果（不 500）。
    """
    failures = list_failures(limit=500)
    incident = None
    for f in failures:
        if f.id == record_id:
            incident = {
                "id": f.id, "target_id": f.target_id, "target_name": f.target_name,
                "url": f.url, "status_code": f.status_code, "duration_ms": f.duration_ms,
                "is_success": f.is_success, "error_msg": f.error_msg,
                "failure_reason": f.failure_reason,
            }
            break

    if incident is None:
        return jsonify({"ok": False, "error": f"记录 {record_id} 不存在", "code": "not_found"})

    result = analyze_incident(incident)
    result["incident"] = incident
    return jsonify(result)


@app.route("/api/summary")
def api_summary():
    """KPI 总览。"""
    return jsonify(summary())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5003, debug=False, threaded=True)
