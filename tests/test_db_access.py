"""阶段 1 测试：只读副本数据访问。"""
from aiops.db_access import (
    DB_PATH,
    _classify_failure,
    list_failures,
    list_targets,
    summary,
)


def test_classify_failure_timeout():
    assert _classify_failure(None, "请求超时: read timed out") == "timeout"


def test_classify_failure_connect():
    assert _classify_failure(None, "连接失败: DNS") == "connect"


def test_classify_failure_exception():
    assert _classify_failure(None, "请求异常: boom") == "exception"


def test_classify_failure_bad_status():
    assert _classify_failure(502, None) == "bad_status"


def test_summary_reads_copy_db():
    s = summary()
    assert s["total"] == 193
    assert s["fail"] == 16
    assert s["success"] == 177
    # 失败分类与审查核实一致：timeout 8 / connect 6 / bad_status 2
    assert s["failure_reasons"].get("timeout") == 8
    assert s["failure_reasons"].get("connect") == 6
    assert s["failure_reasons"].get("bad_status") == 2


def test_list_failures_returns_records():
    failures = list_failures()
    assert len(failures) == 16  # 全部失败记录
    # 每条都有失败分类
    assert all(f.failure_reason for f in failures)


def test_list_targets():
    targets = list_targets()
    assert len(targets) == 4
    names = {t["name"] for t in targets}
    assert {"baidu", "not-exist", "404page", "bing"} <= names


def test_db_path_points_to_copy():
    """数据源是副本，不是 XM01 原库。"""
    assert "xm01_copy.db" in str(DB_PATH)
    assert "XM05" in str(DB_PATH)
