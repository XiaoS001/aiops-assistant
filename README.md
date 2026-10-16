# XM05 · AIOps 智能运维助手

> 定位：把「巡检监控（XM01）」和「RAG + LangGraph 编排（XM03）」合成为一套**可写进运维/SRE 简历**的智能运维项目。
> 技术栈：Flask + LangGraph + LangChain-DeepSeek + SQLite + BM25（jieba 分词）
> 三条底线：不改 XM01/XM03 · 不引 K8s/ES · 不写没跑通的

---

## 一、架构图

```
XM01 巡检发现异常（连接失败 / 超时 / 状态码异常）
        │
        ▼ 生成一条「告警/事件」
AIOps Agent（LangGraph 编排）
        │
        ├─ Supervisor ── 只决定下一步（规则路由，MAX_ROUNDS=12）
        │
        ├─ planner ────── 拆解"这个异常该怎么查"（按失败分类定工具顺序）
        │
        ├─ tool_agent ─── 通过 call_tool 白名单调 5 个工具取证
        │     ├─ query_check_records   查巡检记录（哪次、什么错、耗时多少）
        │     ├─ query_metrics         查指标（成功率 / P95 / 失败分类计数）
        │     ├─ query_logs            按失败分类检索日志摘要（error_msg）
        │     ├─ query_traces          查链路（无存量数据时如实返回空）
        │     └─ search_knowledge      RAG 检索运维知识库（BM25）
        │
        ├─ critic ─────── 判断证据够不够（够 → answerer；不够且未超限 → 继续查）
        │
        └─ answerer ───── 输出「根因分析 + 处置建议」
              ├─ 主路径：调用 DeepSeek 生成（带 [N] 引用）
              ├─ 降级路径：规则模板拼接（无 key / 模型失败时）
              └─ check_citation 校验引用（模型编的引用会触发重答）
        │
        ▼
输出：① 根因分析（每条结论带证据引用）② 处置建议（带引用校验）
```

---

## 二、怎么跑

### 2.1 环境准备（一次性）

```bash
# 1. 建独立 venv（Python 3.14.6）
python -m venv venv

# 2. 装依赖（阿里云源）
venv\Scripts\pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 3. 设 API key（环境变量，绝不写进代码）
set DEEPSEEK_API_KEY=<你的key>
```

### 2.2 启动

```bash
venv\Scripts\python.exe app.py
# 打开 http://127.0.0.1:5003
```

### 2.3 跑测试

```bash
# mock 测试（零 API 消耗）
venv\Scripts\python.exe -m pytest tests -q -p no:warnings

# live 测试（真 key 端到端，默认 skip，需显式 -m live）
venv\Scripts\python.exe -m pytest tests -m live -q -p no:warnings
```

---

## 三、一次真实运行记录（2026-10-16）

### 输入
巡检记录 id=14：目标 `not-exist`（`https://www.zzz-not-exist-12345.com`），
失败分类 `connect`，耗时 7.41ms，错误 `连接失败: HTTPSConnectionPool...Max retries exceeded`

### 工具调用（自动取证）
```
query_check_records(target_id=2) → 5 条记录
query_metrics()                   → 总 193 / 成功率 91.7% / P95 2548ms / 失败{connect:6, timeout:8, bad_status:2}
query_logs(reason=connect)        → 6 条日志摘要
search_knowledge("连接失败")       → 命中 KB-002
query_traces()                    → 无存量链路数据（如实返回）
```

### 模型输出（DeepSeek-V4-Flash，节选）

```
# 根因分析
故障表现为：目标服务 www.zzz-not-exist-12345.com 连接失败 [14][10][8][6][4]
- 总检查 193 次，成功率 91.7%，失败分类 connect 6 / bad_status 2 / timeout 8 [指标]
结合知识库 KB-002：目标域名中带 "not-exist" 字样，疑似不存在 [KB-002]
结论：根因指向巡检配置中包含了不存在的目标域名

# 处置建议
1. 立即修正巡检配置：移除或更正该域名
...
```

### 引用校验
```
citations: [14][10][8][6][4] + [KB-002] → 全部在证据 valid_ids 中 → ok: true
```

---

## 四、JD 映射表

| 岗位要求（真实 JD 原话） | 本项目对应 |
|---|---|
| 「**AI for Ops**：LLM、AI Agent 在运维场景的探索」 | LangGraph 编排 + DeepSeek 生成根因 |
| 「**故障根因分析**」 | 工具取证 → 模型输出根因（带 `[N]` 引用，可核查） |
| 「**智能日志诊断**」 | `query_logs` 按失败分类检索 error_msg（结构化日志摘要） |
| 「**智能告警**」 | 巡检异常 → 自动触发 `analyze_incident` |
| 「**运维问答**」 | 知识库 RAG（BM25 检索 + 处置建议） |
| 「接触过 **LangChain / LangGraph** 框架」 | 直接是 LangGraph 项目（Supervisor + 4 Agent） |
| 「**可观测性**（Prometheus/Grafana/Loki）」 | `query_metrics` 从 XM01 派生指标（成功率/P95/失败分类） |

---

## 五、三条底线如何保证

| 底线 | 保证方式 |
|---|---|
| 不改 XM01/XM03 | 数据源用副本 `data/xm01_copy.db`（只读打开）；复用代码「复制不引用」；每阶段比对 XM01 `app.db` 哈希（基线 `1b4535b6...8484`） |
| 不引重型组件 | 仅 Flask + LangGraph + LangChain + SQLite + BM25，**无 K8s/ES/MQ** |
| 不写没跑通的 | 每阶段测试；模型主路径 + 规则降级路径都已真跑通 |

## 六、目录结构

```
XM05/
├─ README.md            本文件
├─ app.py               Flask 入口（告警列表 / AI 分析）
├─ requirements.txt     依赖（复制自 XM03）
├─ aiops/
│  ├─ db_access.py      只读副本 db + 失败四分类
│  ├─ tools.py          5 个工具 + 白名单 + 参数校验
│  ├─ state.py          AgentState
│  └─ graph.py          LangGraph 编排 + 引用校验 + 降级
├─ knowledge/
│  ├─ kb.json           运维知识库（12 条）
│  └─ retriever.py      BM25 检索
├─ data/
│  └─ xm01_copy.db      XM01 数据副本
├─ templates/
│  └─ index.html        页面（复用 RAE 看板设计系统）
└─ tests/               测试（mock + live）
```
