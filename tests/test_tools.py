"""阶段 3 测试：5 个工具 + 参数校验 + 白名单。"""
from aiops import tools


# ============ 工具能独立调用 ============
def test_tool1_check_records():
    r = tools.query_check_records(limit=5)
    assert r["ok"] is True
    assert r["count"] == 5
    assert all("failure_reason" in rec for rec in r["records"])


def test_tool1_filter_target():
    r = tools.query_check_records(target_id=1, limit=100)
    assert r["ok"] is True
    assert all(rec["target_id"] == 1 for rec in r["records"])


def test_tool2_metrics():
    r = tools.query_metrics()
    assert r["ok"] is True
    assert r["total_checks"] == 193
    assert r["success_rate"] == 91.7
    assert r["p95_duration_ms"] is not None
    assert r["failure_reasons"]["timeout"] == 8


def test_tool3_logs():
    r = tools.query_logs(reason="timeout")
    assert r["ok"] is True
    assert r["count"] > 0
    assert all(log["reason"] == "timeout" for log in r["logs"])


def test_tool4_traces_no_data():
    r = tools.query_traces()
    assert r["ok"] is True
    assert r["has_trace_data"] is False  # 如实说明无存量链路数据
    assert "duration_approx_ms" in r  # 降级用耗时分布近似


def test_tool5_search_knowledge():
    r = tools.search_knowledge("端口占用")
    assert r["ok"] is True
    assert r["count"] > 0
    cats = [x["entry"]["category"] for x in r["results"]]
    assert "port_occupied" in cats


# ============ 参数校验：返回结构化错误，不抛异常 ============
def test_limit_negative_rejected():
    r = tools.query_check_records(limit=-1)
    assert r["ok"] is False
    assert r["code"] == "bad_param"


def test_target_id_string_rejected():
    r = tools.query_check_records(target_id="abc")
    assert r["ok"] is False
    assert r["code"] == "bad_param"


def test_logs_bad_reason_rejected():
    r = tools.query_logs(reason="不存在的分类")
    assert r["ok"] is False
    assert r["code"] == "bad_param"


def test_knowledge_empty_query_rejected():
    r = tools.search_knowledge("")
    assert r["ok"] is False
    assert r["code"] == "bad_param"


def test_knowledge_bad_top_k_rejected():
    r = tools.search_knowledge("超时", top_k=0)
    assert r["ok"] is False
    assert r["code"] == "bad_param"


# ============ 白名单 ============
def test_unknown_tool_rejected():
    r = tools.call_tool("不存在的工具")
    assert r["ok"] is False
    assert r["code"] == "unknown_tool"


def test_list_tools_has_5():
    names = tools.list_tools()
    assert len(names) == 5
    assert {"query_check_records", "query_metrics", "query_logs",
            "query_traces", "search_knowledge"} <= set(names)


def test_call_tool_via_whitelist():
    r = tools.call_tool("query_metrics")
    assert r["ok"] is True


def test_describe_tools():
    desc = tools.describe_tools()
    assert "query_check_records" in desc
    assert "search_knowledge" in desc
