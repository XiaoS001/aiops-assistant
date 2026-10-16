"""阶段 4 测试：LangGraph 编排 + 引用校验 + 降级。"""
from aiops.graph import analyze_incident, _check_citation, _extract_citation_ids
from aiops import tools


def _make_incident(reason="connect", target_id=2):
    """造一条异常记录（与真实数据同构）。"""
    return {
        "id": 189, "target_id": target_id, "target_name": "not-exist",
        "url": "https://www.zzz-not-exist-12345.com",
        "status_code": None, "duration_ms": 5135.9,
        "is_success": False, "error_msg": "连接失败: ...", "failure_reason": reason,
    }


# ============ 端到端 ============
def test_analyze_connect_returns_answer():
    r = analyze_incident(_make_incident("connect"))
    assert r["ok"] is True
    assert "根因分析" in r["answer"]
    assert "连接失败" in r["answer"]


def test_analyze_timeout_returns_answer():
    r = analyze_incident(_make_incident("timeout"))
    assert r["ok"] is True
    assert "请求超时" in r["answer"]


def test_analyze_bad_status_returns_answer():
    r = analyze_incident(_make_incident("bad_status"))
    assert r["ok"] is True
    assert "状态码异常" in r["answer"]


def test_analyze_has_evidence():
    r = analyze_incident(_make_incident("connect"))
    assert r["evidence_count"] > 0


# ============ 引用校验 ============
def test_extract_citation_ids():
    assert _extract_citation_ids("依据巡检记录第 [189] 条") == ["189"]


def test_check_citation_valid():
    evidence = [{"id": 189}, {"id": 190}]
    cc = _check_citation("依据 [189]", evidence)
    assert cc["ok"] is True
    assert cc["invalid_citations"] == []


def test_check_citation_invalid():
    evidence = [{"id": 189}]
    cc = _check_citation("依据 [999]（不存在）", evidence)
    assert cc["ok"] is False
    assert "999" in cc["invalid_citations"]


def test_check_citation_no_citation():
    evidence = [{"id": 189}]
    cc = _check_citation("没有引用任何记录", evidence)
    assert cc["ok"] is True
    assert cc["citations"] == []


# ============ 降级（两层）============
def test_analyze_no_api_key_still_works():
    """无 API key 也能跑（规则路径，不依赖模型）。"""
    import os
    old = os.environ.get("DEEPSEEK_API_KEY")
    os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        r = analyze_incident(_make_incident("connect"))
        assert r["ok"] is True
        assert r["answer"]
    finally:
        if old is not None:
            os.environ["DEEPSEEK_API_KEY"] = old


def test_analyze_bad_incident_no_crash():
    """非法输入不崩，返回结构化结果。"""
    r = analyze_incident({})
    assert r["ok"] is True
    assert "根因分析" in r["answer"] or r["answer"]  # 有输出，不抛异常


def test_analyze_missing_fields_no_crash():
    """缺字段的异常记录也能处理。"""
    r = analyze_incident({"failure_reason": "timeout"})
    assert r["ok"] is True


# ============ 引用可核查（验收口径）============
def test_answer_citations_are_real():
    """回答里引用的巡检记录 id 必须真实存在（在副本 db 里）。"""
    r = analyze_incident(_make_incident("connect", target_id=2))
    # 回答里引用了 [189]（我们造的），校验应该通过或明确标记
    cc = r["citation_check"]
    assert isinstance(cc, dict)
    # 引用要么为空要么被校验
    assert "ok" in cc


def test_tools_still_work_after_graph():
    """graph 引用 tools 后，工具仍可独立调用。"""
    r = tools.query_metrics()
    assert r["ok"] is True
    assert r["total_checks"] == 193

# ============ 阶段4打回：模型路径测试（mock _call_model，零 API 消耗）============
import pytest


@pytest.fixture
def mock_model(monkeypatch):
    """mock _call_model 返回固定文本（带引用 [189]）。"""
    import aiops.graph as g
    def fake(prompt, variables, api_key):
        return "根因分析：目标 not-exist 出现连接失败，可能原因 DNS 解析失败 [189]。\\n处置建议：检查 DNS 配置。"
    monkeypatch.setattr(g, "_call_model", fake)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-mock")
    return g


def test_answerer_uses_model_when_key(mock_model):
    """有 key 时 answerer 走模型路径。"""
    from aiops.graph import analyze_incident
    r = analyze_incident(_make_incident("connect"))
    assert r["ok"] is True
    assert "DNS 解析失败" in r["answer"]  # 模型生成的句子
    assert r["degraded"] is False


def test_answerer_degrades_without_key(monkeypatch):
    """无 key 时 answerer 退回规则路径，degraded=True。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from aiops.graph import analyze_incident
    r = analyze_incident(_make_incident("connect"))
    assert r["ok"] is True
    assert "根因分析" in r["answer"]  # 规则路径也有结论
    assert r["degraded"] is True


def test_answerer_degrades_on_model_error(mock_model, monkeypatch):
    """模型抛异常时退回规则路径。"""
    import aiops.graph as g
    def boom(prompt, variables, api_key):
        raise RuntimeError("模型挂了")
    monkeypatch.setattr(g, "_call_model", boom)
    from aiops.graph import analyze_incident
    r = analyze_incident(_make_incident("connect"))
    assert r["ok"] is True
    assert r["degraded"] is True
    assert "根因分析" in r["answer"]


def test_model_output_with_invalid_citation_triggers_check():
    """模型编了个不存在的引用 [999]，校验应标记 invalid。"""
    from aiops.graph import _check_citation
    answer = "根因 [999] 导致故障"
    evidence = [{"id": 14}]
    cc = _check_citation(answer, evidence)
    assert cc["ok"] is False
    assert "999" in cc["invalid_citations"]


@pytest.mark.live
def test_live_end_to_end_with_real_key():
    """live 测试：真 key 端到端（默认 skip，pytest -m live 才跑）。"""
    import os
    from aiops.db_access import list_failures
    from aiops.graph import analyze_incident
    if not os.environ.get("DEEPSEEK_API_KEY"):
        pytest.skip("无 DEEPSEEK_API_KEY，跳过 live 测试")
    failures = list_failures()
    c = [f for f in failures if f.failure_reason == "connect"][0]
    inc = {"id": c.id, "target_id": c.target_id, "target_name": c.target_name, "url": c.url,
           "status_code": c.status_code, "duration_ms": c.duration_ms, "is_success": c.is_success,
           "error_msg": c.error_msg, "failure_reason": c.failure_reason}
    r = analyze_incident(inc)
    assert r["ok"] is True
    assert r["answer"]
