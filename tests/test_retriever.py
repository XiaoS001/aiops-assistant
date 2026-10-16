"""阶段 2 测试：知识库检索（BM25）。"""
from knowledge.retriever import build_index, search


def test_build_index_12_entries():
    assert build_index() == 12


def test_search_timeout_hits_timeout_entry():
    results = search("服务超时")
    assert results, "应检索到超时条目"
    cats = [r["entry"]["category"] for r in results]
    assert "timeout" in cats, f"timeout 应在命中列表: {cats}"


def test_search_connect_hits_connect_entry():
    results = search("连接失败")
    assert results
    top = results[0]["entry"]
    assert top["category"] == "connect"


def test_search_disk_hits_disk_entry():
    results = search("磁盘空间不足")
    assert results
    cats = [r["entry"]["category"] for r in results]
    assert "disk_full" in cats


def test_search_port_hits_port_entry():
    results = search("端口占用")
    assert results
    cats = [r["entry"]["category"] for r in results]
    assert "port_occupied" in cats


def test_search_5xx_hits_5xx_entry():
    results = search("服务返回500")
    assert results
    cats = [r["entry"]["category"] for r in results]
    assert "5xx" in cats


def test_search_no_match_returns_empty():
    """知识库没有的词 → 返回空列表，不报错。"""
    results = search("量子纠缠态坍缩")
    assert results == []


def test_search_empty_query_returns_empty():
    assert search("") == []


def test_result_has_score_and_entry():
    results = search("超时")
    assert results
    for r in results:
        assert "entry" in r
        assert "score" in r
        assert isinstance(r["score"], (int, float))


def test_top_k_limit():
    results = search("服务", top_k=2)
    assert len(results) <= 2
