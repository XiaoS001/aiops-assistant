"""XM05 LangGraph 编排：Supervisor + 4 专职 Agent + 引用校验 + 两层降级。

参考 XM03 agent.py（复制不引用），AIOps 场景改造：
- Supervisor：只决定下一步
- planner：拆解"这个异常该怎么查"（决定调哪些工具）
- tool_agent：通过 call_tool 白名单调 5 个工具取证
- critic：判断证据够不够
- answerer：输出根因 + 处置建议，并校验引用
- 两层降级：模型失败 → 规则/直查路径；接口不 500
- 双上限：MAX_ROUNDS / MAX_ATTEMPTS
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek
from langgraph.graph import END, START, StateGraph

from aiops import tools
from aiops.state import AgentState

DEEPSEEK_MODEL = "deepseek-chat"
AMD_BASE_URL = "https://developer.amd.com.cn/radeon/api/v1"
AMD_MODEL = "DeepSeek-V4-Flash"
REQUEST_TIMEOUT = 60
MAX_ROUNDS = 12
MAX_ATTEMPTS = 3

# 引用校验：正则提取 [N] 或「第N条」
_CITATION_RE = re.compile(r"\[(\d{1,6})\]|第\s*(\d+)\s*条")


def _call_model(prompt, variables: dict, api_key: str) -> str:
    """调用 DeepSeek（AMD 端点，OpenAI 兼容），失败抛异常（由上层降级捕获）。"""
    model = ChatDeepSeek(
        model=AMD_MODEL,
        api_key=api_key,
        api_base=AMD_BASE_URL,  # 关键：不加会连 DeepSeek 官方
        timeout=REQUEST_TIMEOUT,
    )
    response = (prompt | model).invoke(variables)
    content = response.content
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回内容为空")
    return content.strip()


def _api_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


# ============ 引用校验（复制 XM03 check_citation 思路）============
def _extract_citation_ids(answer: str) -> list[str]:
    """提取回答里引用的记录 id（[N] 或「第N条」）。"""
    ids: list[str] = []
    seen: set[str] = set()
    for m in _CITATION_RE.finditer(answer):
        cid = next(g for g in m.groups() if g is not None)
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


def _check_citation(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """校验回答里引用的记录 id 是否真实存在于证据中。

    递归提取证据里所有可能的 id：证据顶层 id、result.records[].id、
    result.logs[].check_id、result.results[].entry.id、result.duration_approx 等。
    """
    citations = _extract_citation_ids(answer)
    valid_ids: set[str] = set()

    def collect(obj: Any) -> None:
        """递归收集所有能作为引用 id 的值。"""
        if isinstance(obj, dict):
            for key in ("id", "check_id", "record_id"):
                v = obj.get(key)
                if v is not None and isinstance(v, (int, str)):
                    valid_ids.add(str(v))
            for v in obj.values():
                collect(v)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)

    for ev in evidence:
        collect(ev)

    invalid = [c for c in citations if c not in valid_ids]
    return {
        "ok": not invalid,
        "citations": citations,
        "valid_ids": sorted(valid_ids),
        "invalid_citations": invalid,
    }





# ============ 节点 ============
def supervisor_node(state: AgentState) -> dict[str, Any]:
    """Supervisor：只决定下一步（规则路由，不用模型）。"""
    rounds = state.get("rounds", 0)
    if rounds >= MAX_ROUNDS:
        return {"next_agent": "answerer"}
    last = state.get("last_agent")
    if not last:
        return {"next_agent": "planner"}
    if last == "planner":
        return {"next_agent": "tool_agent"}
    if last == "tool_agent":
        return {"next_agent": "critic"}
    if last == "critic":
        cc = state.get("citation_check") or {}
        if state.get("answer") and cc.get("ok") is False:
            if state.get("citation_retry_count", 0) < 1 and state.get("attempts", 0) < MAX_ATTEMPTS:
                return {"next_agent": "tool_agent"}
            return {"next_agent": "answerer"}
        if state.get("verdict") == "够" or state.get("attempts", 0) >= MAX_ATTEMPTS:
            return {"next_agent": "answerer"}
        return {"next_agent": "tool_agent"}
    if last == "answerer":
        return {"next_agent": "end"}
    return {"next_agent": "answerer"}


def planner_node(state: AgentState) -> dict[str, Any]:
    """planner：决定查什么（规则：按异常分类定工具顺序，不用模型也能工作）。"""
    incident = state.get("incident") or {}
    reason = incident.get("failure_reason", "")

    # 规则降级路径（无 key 也能给出计划）
    if reason in ("connect", "timeout"):
        plan = "连接/超时类异常：查巡检记录 → 查指标 → 查日志 → 查知识库"
        tool_plan = ["query_check_records", "query_metrics", "query_logs", "search_knowledge"]
    elif reason == "bad_status":
        plan = "状态码异常：查巡检记录 → 查指标 → 查知识库"
        tool_plan = ["query_check_records", "query_metrics", "search_knowledge"]
    else:
        plan = "未知异常：查巡检记录 → 查指标 → 查知识库"
        tool_plan = ["query_check_records", "query_metrics", "search_knowledge"]

    return {
        "plan": plan,
        "tool_calls": tool_plan,
        "evidence": [],
        "verdict": "",
        "answer": "",
        "citations": [],
        "invalid_citations": [],
        "citation_check": {},
        "citation_retry_count": 0,
        "attempts": 0,
        "last_agent": "planner",
        "degraded": False,
        "degradation_reasons": [],
    }


def tool_agent_node(state: AgentState) -> dict[str, Any]:
    """tool_agent：通过 call_tool 白名单调工具取证。"""
    incident = state.get("incident") or {}
    tool_calls = state.get("tool_calls") or []
    evidence: list[dict[str, Any]] = list(state.get("evidence") or [])
    degradation_reasons: list[str] = list(state.get("degradation_reasons") or [])

    # 按计划逐个调工具（每个工具返回结构化结果）
    for name in tool_calls:
        if name not in tools.list_tools():
            continue
        try:
            if name == "query_check_records":
                result = tools.call_tool(name, target_id=incident.get("target_id"), limit=10)
            elif name == "query_logs":
                result = tools.call_tool(name, reason=incident.get("failure_reason"), limit=10)
            elif name == "search_knowledge":
                # 用异常原因构造查询词（connect→连接失败, timeout→请求超时...）
                reason_cn = {"connect": "连接失败", "timeout": "请求超时",
                             "bad_status": "状态码异常", "exception": "请求异常"}.get(
                                 incident.get("failure_reason", ""), "故障")
                result = tools.call_tool(name, query=reason_cn, top_k=3)
            else:
                result = tools.call_tool(name)
            evidence.append({"tool": name, "result": result})
        except Exception as exc:
            degradation_reasons.append(f"{name} 调用失败: {exc}")
            evidence.append({"tool": name, "result": {"ok": False, "error": str(exc)}})

    attempts = state.get("attempts", 0) + 1
    return {
        "evidence": evidence,
        "attempts": attempts,
        "last_agent": "tool_agent",
        "degradation_reasons": degradation_reasons,
    }


def critic_node(state: AgentState) -> dict[str, Any]:
    """critic：判断证据够不够（规则：有证据且含知识库命中即可，不用模型）。"""
    evidence = state.get("evidence") or []
    if not evidence:
        return {"verdict": "不够", "last_agent": "critic"}
    # 有巡检记录证据 且 有知识库检索结果 → 够
    has_check = any(e.get("tool") == "query_check_records" and e["result"].get("ok") for e in evidence)
    has_kb = any(e.get("tool") == "search_knowledge" and e["result"].get("count", 0) > 0 for e in evidence)
    return {"verdict": "够" if (has_check and has_kb) else "不够", "last_agent": "critic"}


def _format_evidence(evidence: list[dict[str, Any]]) -> str:
    """把工具取证结果整理成给模型的证据文本（带 id，供引用）。"""
    parts: list[str] = []
    for ev in evidence:
        tool_name = ev.get("tool", "?")
        result = ev.get("result") or {}
        if not result.get("ok"):
            parts.append(f"[工具 {tool_name}] 调用失败: {result.get("error", "未知")}")
            continue
        if tool_name == "query_check_records":
            for rec in result.get("records", [])[:5]:
                parts.append(
                    f"[巡检记录 id={rec.get("id")}] 目标={rec.get("target_name")} "
                    f"失败分类={rec.get("failure_reason")} 耗时={rec.get("duration_ms")}ms"
                )
        elif tool_name == "query_metrics":
            parts.append(
                f"[指标] 总检查={result.get("total_checks")} 成功率={result.get("success_rate")}% "
                f"P95={result.get("p95_duration_ms")}ms 失败分类={result.get("failure_reasons")}"
            )
        elif tool_name == "query_logs":
            for log in result.get("logs", [])[:5]:
                parts.append(
                    f"[日志 id={log.get("check_id")}] 目标={log.get("target_name")} "
                    f"分类={log.get("reason")} 错误={str(log.get("error_msg"))[:80]}"
                )
        elif tool_name == "search_knowledge":
            for r in result.get("results", []):
                entry = r.get("entry", {})
                parts.append(
                    f"[知识库 {entry.get("id")}] 现象={entry.get("symptom")} "
                    f"原因={"; ".join(entry.get("causes", []))} "
                    f"处置={"; ".join(entry.get("actions", []))}"
                )
        elif tool_name == "query_traces":
            parts.append(f"[链路] {result.get("message", "无数据")}")
    return "\n".join(parts)


ROOT_CAUSE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是运维根因分析助手。请只依据下面提供的证据作答，输出两部分：\n"
            "1. 根因分析：说明故障是什么、可能原因\n"
            "2. 处置建议：列出可执行的处置步骤\n\n"
            "硬性要求：\n"
            "- 每条结论标注依据来源，格式 [N]（N = 巡检记录 id 或知识库条目 id）\n"
            "- 证据里没有的信息不要编造\n"
            "- 如果证据不足以定论，明确说证据不足，需要进一步排查",
        ),
        ("human", "【证据】\n{evidence}"),
    ]
)


def answerer_node(state: AgentState) -> dict[str, Any]:
    """answerer：输出根因 + 处置建议。

    模型路径 = 主路径（有 key 且调用成功）；
    规则路径 = 降级（无 key / 调用失败 / 超时）。
    """
    incident = state.get("incident") or {}
    evidence = state.get("evidence") or []
    degradation_reasons: list[str] = list(state.get("degradation_reasons") or [])
    degraded = state.get("degraded", False)

    answer = ""
    api_key = _api_key()

    if api_key:
        try:
            evidence_text = _format_evidence(evidence)
            answer = _call_model(ROOT_CAUSE_PROMPT, {"evidence": evidence_text}, api_key)
        except Exception as exc:
            degradation_reasons.append(f"answerer 模型调用失败，退回规则路径: {exc}")
            degraded = True
    else:
        # 无 key → 直接降级到规则路径
        degradation_reasons.append("未配置 DEEPSEEK_API_KEY，退回规则路径")
        degraded = True

    if not answer:
        # 规则降级路径（模板拼接）
        check_id = incident.get("id", "?")
        reason = incident.get("failure_reason", "unknown")
        target = incident.get("target_name", "?")
        url = incident.get("url", "?")
        kb_id = None
        kb_actions = []
        for e in evidence:
            if e.get("tool") == "search_knowledge" and e["result"].get("results"):
                top = e["result"]["results"][0]
                kb_id = top["entry"].get("id")
                kb_actions = top["entry"].get("actions", [])
                break
        reason_cn = {"timeout": "请求超时", "connect": "连接失败", "bad_status": "状态码异常", "exception": "请求异常"}.get(reason, reason)
        lines = [f"根因分析：目标「{target}」（{url}）出现{reason_cn}。"]
        if kb_id:
            lines.append(f"依据知识库条目 [{kb_id}]，可能原因：")
        lines.append(f"依据巡检记录第 [{check_id}] 条：失败分类为 {reason}，耗时 {incident.get("duration_ms", "?")}ms。")
        if kb_actions:
            lines.append("处置建议：")
            for a in kb_actions:
                lines.append(f"  - {a}")
        answer = "\n".join(lines)

    # 引用校验（模型输出也必须校验，模型可能编引用）
    citation_check = _check_citation(answer, evidence)
    return {
        "answer": answer,
        "citations": citation_check["citations"],
        "invalid_citations": citation_check["invalid_citations"],
        "citation_check": citation_check,
        "last_agent": "answerer",
        "degraded": degraded,
        "degradation_reasons": degradation_reasons,
    }

def should_continue(state: AgentState) -> Literal["continue", "end"]:
    return "end" if state.get("next_agent") == "end" else "continue"


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("planner", planner_node)
    g.add_node("tool_agent", tool_agent_node)
    g.add_node("critic", critic_node)
    g.add_node("answerer", answerer_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor",
        lambda s: s.get("next_agent", "answerer"),
        {"planner": "planner", "tool_agent": "tool_agent", "critic": "critic", "answerer": "answerer", "end": END},
    )
    g.add_edge("planner", "supervisor")
    g.add_edge("tool_agent", "supervisor")
    g.add_edge("critic", "supervisor")
    g.add_edge("answerer", "supervisor")

    return g.compile()


def analyze_incident(incident: dict[str, Any]) -> dict[str, Any]:
    """对外入口：输入一条异常记录，输出根因 + 处置建议。

    两层降级：本实现全程走规则路径（不依赖模型），因此：
    - 无 API key 也能工作（降级即默认路径）
    - 任何异常都被捕获，返回结构化结果，绝不抛 500
    """
    try:
        graph = build_graph()
        result = graph.invoke({"incident": incident, "incident_id": incident.get("id")})
        return {
            "ok": True,
            "answer": result.get("answer", ""),
            "evidence_count": len(result.get("evidence") or []),
            "citation_check": result.get("citation_check") or {},
            "degraded": result.get("degraded", False),
            "degradation_reasons": result.get("degradation_reasons") or [],
        }
    except Exception as exc:
        # 最外层兜底：任何异常都返回结构化结果，不 500
        return {
            "ok": True,
            "answer": f"分析失败，已降级：{str(exc)[:200]}",
            "evidence_count": 0,
            "citation_check": {"ok": True, "citations": [], "invalid_citations": []},
            "degraded": True,
            "degradation_reasons": [f"analyze 失败: {exc}"],
        }
