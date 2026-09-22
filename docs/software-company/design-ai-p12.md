# 架构设计：AI 原生 BI 平台 — AI 能力扩展（增量 P1+P2）

> 文档类型：增量架构设计 + 任务分解｜架构师：高见远
> 配套 PRD：`bi-platform/docs/software-company/prd-ai-p12.md`
> 配套基线：`backend/`（FastAPI+SQLAlchemy+SQLite+DuckDB）、`web/`（Vue3+Element Plus+Pinia+Vite）

---

## 0. 设计总览与核心决策

### 0.1 两条红线（贯穿全部六项，设计强制保证）
- **算写分离**：三个新的叙述类调用点（看板速览 / 异动假设 / 归因解读）**复用同一套**占位符 + 服务端预格式化回填 + 三道审计设施，LLM 永远只见 `{{ref:KEY}}` 与业务含义标签，零原始数值。
- **LLM 可选降级**：所有新功能在 `resolve_llm_config()` 返回 `None` 或 `chat_json` 返回 `None`（失败/超时）时优雅降级，且任何 LLM 异常均被 `try/except` 静默吞掉，**不得** 上抛导致 500。

### 0.2 关键设计决策（已对照主理人裁决）
1. **通用算写分离引擎独立成模块** `app/domain/ai_narrative.py`：抽出 `PLACEHOLDER_RE`、`BARE_DIGIT_RE`、`audit_sentence`（三道审计①②）、`backfill`（回填）、`llm_narrative`（通用叙述生成）。`report/narrative.py` 改为**消费方**（从本模块导入原语，不再各自重定义），保证报告现有行为与测试零破坏。
2. **看板速览触发方式**：手动按钮触发；前端按 `project_id + 周期` 会话内缓存，同周期重复点击直接复用；LLM 未配置时**按钮隐藏**并展示规则化一句概述（"本期共 N 个指标，X 个环比上升、Y 个下降"）。
3. **异动假设不引入业务日历**：只基于平台已有的常用维度拆解事实（TopN 贡献 + 方向一致性），范围严格收敛到 `metric.dimensions` 前 2 层、每层 Top3。
4. **字段标注落库**：`datasets` 表新增 JSON 列 `column_semantics_json` 存标注（幂等 ALTER 迁移，复用 `migrate_project_columns` 模式）；重新标注覆盖；标注为空不影响任何现有功能。
5. **问数推荐隔离**：沿用 `restricted_metric_ids` + `project_id` 过滤（与问数锁定项目语义一致），只是推荐生成逻辑升级；数字经服务端预格式化（`formatPercent`）后才入文案。
6. **归因解读只做第一层**：只对下钻树根节点（`path=[]`）的第一层 TopN 贡献生成一句解读，不递归全树；与现有 `attribute_tree_node` **解耦**（独立端点复算根节点，不污染既有下钻）。
7. **LLM 调用预算**：统一 `timeout=60s`、`retries=0`（不重试）。`chat_json` 新增可选 `retries` 形参，**默认保持 1**（现有报告/建模/问数行为不变），仅六个新调用点显式传 `retries=0`，既对齐裁决"新调用点不重试"，又不破坏现有调用方。

### 0.3 实现原则（与基线一致）
- 新接口一律走既有 `ApiResponse` 包装（`ok_response`）；新叙述调用点经 `app/domain/ai_narrative.py`。
- 所有新接口按 `project_id` 过滤（B9.3 契约：禁止跨项目数据）。
- 数字展示前端统一 `formatMetricValue` / `formatPercent`（1 位小数千分位），后端回填文本也用等价格式化（`f"{v:,.1f}"` / `f"{p:.1f}%"`），保证前后端对账一致；涨跌配色由 `TrendBadge`/ `--pwc-up`/`--pwc-down` 决定，与 good/bad 解耦。
- 名词定义引用 `web/src/constants/glossary.js`（新增词条：`ai_overview`、`ai_hypothesis`、`ai_calc_notes`、`semantic_annotation` 等，详见 §7）。

---

## 1. 实现方案：新增 / 修改文件清单

### 1.1 后端新增文件
| 相对路径 | 作用 |
|---|---|
| `backend/app/domain/ai_narrative.py` | **通用算写分离引擎**（核心复用点）：`PLACEHOLDER_RE`、`BARE_DIGIT_RE`、`audit_sentence`、`backfill`、`llm_narrative`、`fmt_metric_value`、`fmt_pct`。所有叙述类调用点（含报告）共用。 |
| `backend/app/domain/ai/dashboard_summary.py` | 功能 #1：看板速览 service `build_dashboard_summary(db, user, project_id, start, end, metric_ids=None)`。 |
| `backend/app/domain/ai/anomaly_hypothesis.py` | 功能 #2：异动假设 service `build_anomaly_hypothesis(db, user, metric_id, start, end, compare="mom", dimensions=None)`。 |
| `backend/app/domain/ai/calc_notes.py` | 功能 #3：口径助手 service `suggest_calc_notes(db, dataset_id, column, aggregation, alias=None, sample_values=None)`。 |
| `backend/app/domain/ai/semantic_annotations.py` | 功能 #4：字段标注 service `suggest_semantic_annotations(db, dataset_id)`、`save_semantic_annotations(db, dataset_id, mapping)`。 |
| `backend/app/domain/ai/attribute_interpretation.py` | 功能 #6：归因解读 service `build_attribute_interpretation(db, user, metric_id, start, end, dimensions, compare="mom")`。 |
| `backend/app/api/routes/ai.py` | 新路由 `router = APIRouter(prefix="/ai", tags=["ai"])`，承载六项新接口（§3）。 |
| `backend/tests/test_ai_narrative.py` | 引擎单测：占位符审计、回填、LLM 缺配/失败降级（monkeypatch `app.domain.ai_narrative.chat_json`）。 |
| `backend/tests/test_ai_features.py` | 六功能集成/单测（monkeypatch LLM 注入，离线可回归）。 |

### 1.2 后端修改文件
| 相对路径 | 改动点 |
|---|---|
| `backend/app/infra/llm.py` | `chat_json` 新增可选形参 `retries: int = 1`（默认 1 保持现状；新调用点传 0）。其余不变。 |
| `backend/app/domain/report/narrative.py` | 改为消费方：从 `app.domain.ai_narrative` 导入 `PLACEHOLDER_RE`/`BARE_DIGIT_RE`/`audit_sentence`/`backfill`（删除本地重定义，避免漂移）；保留 `_build_ref_table`（报告专属）与 `llm_narrative`/`try_narrative`，但其内部复用导入的 `audit_sentence`/`backfill`。**行为与测试不变**。 |
| `backend/app/infra/models.py` | `Dataset` 模型新增 `column_semantics_json: Mapped[str] = mapped_column(Text, default="{}")`。 |
| `backend/app/infra/database.py` | `migrate_project_columns` 对 `datasets` 表幂等 `ALTER TABLE ... ADD COLUMN column_semantics_json TEXT DEFAULT '{}'`。 |
| `backend/app/api/routes/ask.py` 或 `backend/app/domain/ask/service.py` | 功能 #5：升级 `build_suggestions`，注入"最近异动"动态推荐（§3 接口不变，仅 service 逻辑升级）。 |
| `backend/app/main.py` | 注册新路由：`from app.api.routes import ai` + `application.include_router(ai.router, prefix=settings.api_prefix)`。 |
| `backend/tests/test_narrative.py`（如存在） | 确认报告回归不受影响（仅需跑通，无需改）。 |

### 1.3 前端新增 / 修改文件
| 相对路径 | 改动点 |
|---|---|
| `web/src/api/ai/index.js`（新增） | 六项新接口的 request 封装（`dashboardSummary`、`anomalyHypothesis`、`suggestCalcNotes`、`semanticAnnotations`、`saveSemanticAnnotations`、`attributeInterpretation`）。 |
| `web/src/views/dashboard/Dashboard.vue`（改） | 新增"AI 速览"按钮区 + 速览结果卡；按 `project_id+周期` 会话内缓存；`llm_configured=false` 时隐藏按钮并显示规则句。 |
| `web/src/views/metric/MetricAdmin.vue`（改） | 计算规则区新增"AI 帮写口径"按钮，回填 `name`/`aliases`/`calc_notes`（用户可编辑，绝不自动保存）；未配置 AI 时按钮禁用并提示。 |
| `web/src/views/metric/MetricDetail.vue`（改） | 异动卡（基于现有节点概要区或新增区块）展示异动假设；归因下钻根节点下方展示 AI 一句话解读。 |
| `web/src/views/dataset/DatasetManage.vue`（改，P2） | 数据集详情/列览新增"AI 标注字段含义"按钮与标注展示（调用 #4）。 |
| `web/src/constants/glossary.js`（改） | 新增词条：`ai_overview`、`ai_hypothesis`、`ai_calc_notes`、`semantic_annotation`、`ask_dynamic_suggestion`（名词单一数据源）。 |
| `web/src/views/ask/Ask.vue`（改，P2） | 空态推荐文案由后端动态提供（接口不变，前端已支持 `suggestions` 数组，无需大改）。 |

---

## 2. 通用算写分离引擎设计（`app/domain/ai_narrative.py`）

```python
"""通用算写分离引擎（增量 P1+P2 共享，报告 narrative.py 亦复用）。
红线：LLM 只见 {{ref:KEY}} + 业务含义标签；回填文本在服务端预格式化；
三道审计：① 未知 ref 剔句 ② 去占位符后残留数字剔句 ③ 全剔/失败→降级(由调用方决定)。
"""
PLACEHOLDER_RE = re.compile(r"\{\{ref:([A-Za-z0-9_]+)\}\}")
BARE_DIGIT_RE = re.compile(r"\d")  # 去占位符后不允许任何数字

def audit_sentence(sentence: str, ref_keys: set[str]) -> bool:
    """三道审计之①②：未知 ref / 裸数字 → 不合格。"""
    keys = PLACEHOLDER_RE.findall(sentence)
    if any(k not in ref_keys for k in keys):
        return False
    return BARE_DIGIT_RE.search(PLACEHOLDER_RE.sub("", sentence)) is None

def backfill(sentence: str, table: dict[str, tuple[str, str]]) -> str:
    """table: key -> (业务含义标签, 服务端预格式化显示文本)。"""
    return PLACEHOLDER_RE.sub(lambda m: table[m.group(1)][1], sentence)

def fmt_metric_value(v) -> str:
    return f"{float(v):,.1f}" if v is not None else "—"

def fmt_pct(p) -> str:  # p 已是百分数单位（*100 后），与前端 formatPercent 对齐
    return f"{float(p):.1f}%" if p is not None else "—"

def llm_narrative(db, settings, *, ref_table, sections_to_write, system_prompt,
                  user_payload, timeout: float = 60.0, retries: int = 0) -> dict | None:
    """通用叙述生成。ref_table 由调用方构建（key -> (标签, 显示文本)）。
    返回 {sections:[{section,sentences,source}], source, degraded} 或 None（调用方降级）。
    未配置 LLM / JSON 异常 / 全部被剔除 → None。失败不抛异常。"""
    config = resolve_llm_config(db, settings)
    if not config:
        return None
    user_payload = {**user_payload,
                    "available_refs": {k: v[0] for k, v in ref_table.items()}}
    obj = chat_json(config, system_prompt,
                    json.dumps(user_payload, ensure_ascii=False),
                    timeout=timeout, retries=retries)
    if not isinstance(obj, dict) or not isinstance(obj.get("sections"), list):
        return None
    ref_keys = set(ref_table)
    rule_fallback = {s["section"]: s["sentences"]
                    for s in user_payload.get("rule_narrative", [])}
    out, degraded, used_llm = [], [], False
    for key in sections_to_write:
        sec = next((s for s in obj["sections"]
                    if isinstance(s, dict) and s.get("section") == key), None)
        sentences = [s.strip() for s in (sec or {}).get("sentences", [])
                     if isinstance(s, str) and s.strip()]
        kept = [backfill(s, ref_table) for s in sentences
                if audit_sentence(s, ref_keys)]
        if kept:
            out.append({"section": key, "sentences": kept, "source": "llm"})
            used_llm = True
        elif key in rule_fallback:
            out.append({"section": key, "sentences": rule_fallback[key], "source": "rule"})
            degraded.append(key)
    return {"sections": out, "source": "llm", "degraded": degraded} if used_llm else None
```

**报告侧改造（不破坏测试）**：`report/narrative.py` 删除本地 `PLACEHOLDER_RE`/`BARE_DIGIT_RE`/`_audit_sentence`/`_backfill` 定义，改为 `from app.domain.ai_narrative import (PLACEHOLDER_RE, BARE_DIGIT_RE, audit_sentence as _audit_sentence, backfill as _backfill)`；保留 `_build_ref_table`（报告专属，仍从 `report.service` 取 `_fmt_pct`/`_n` 或改用 `ai_narrative.fmt_*`）、`llm_narrative`、`try_narrative`，其中审计/回填调用导入的原语。`chat_json` 仍由 `report.narrative` 顶层 `from app.infra.llm import chat_json, resolve_llm_config` 导入（现有 monkeypatch 测试命名空间不变）。

---

## 3. 新接口定义（统一 `ApiResponse`，前缀 `/ai`）

> 全部接口需登录（`CurrentUser`）；涉及项目的按 `project_id` 过滤（B9.3）。所有 LLM 调用 `timeout=60, retries=0`，异常静默降级。

### 3.1 #1 看板智能摘要  `GET /ai/dashboard-summary`
- **Query**：`project_id: int|null`、`start: str(YYYY-MM-DD)`、`end: str`、`metric_ids: list[int]|null`（可选，限定看板当前可见卡片；缺省取项目内 active 且可见指标，封顶 15）。
- **响应 data**：
```json
{
  "llm_configured": true,
  "source": "llm" | "rule" | "none",
  "sections": [{"section":"overview","sentences":["..."],"source":"llm"}],
  "degraded": [],
  "rule_text": "本期共 12 个指标，7 个环比上升、5 个下降"   // source!=llm 时前端展示
}
```
- **降级**：`llm_configured=false` → `source="none"`，返回 `rule_text`（规则计数句，零查询或仅轻量计数），前端**隐藏按钮**。LLM 失败/全部剔除 → `source="rule"`，返回 `rule_text` 或规则句。

### 3.2 #2 异动 AI 假设解释  `GET /ai/anomaly-hypothesis`
- **Query**：`metric_id: int`、`start`、`end`、`compare="mom"`、`dimensions: list[str]|null`（缺省取 `metric.dimensions[:2]`）。
- **逻辑**：先 `detect_for_metric(metric, detect_date=end)` → 非 abnormal 或 `abnormal and material is False` → 返回 `{"has_anomaly":false}`（前端不展示假设）；否则对 `dimensions` 各层调用 `attribute_delta`（窗口=传入周期）取 Top3 贡献 + 方向一致/背离事实，构建 ref_table（贡献占比占位符 + 维度名/贡献者名字符串），LLM 生成 1~2 句业务假设。
- **响应 data**：`{"has_anomaly":true,"direction":"up|down","hypothesis":["..."],"source":"llm|rule","fallback_action_hint":"...","llm_configured":bool}`。降级：`llm_configured=false` 或失败 → `hypothesis=[]` + `fallback_action_hint`（沿用 `_action_hint` 固定文案），前端只展示固定文案。

### 3.3 #3 指标口径 AI 助手  `POST /ai/metric-calc-notes`
- **Body**：`{ "dataset_id": int, "column": str, "aggregation": str, "alias": str|null, "sample_values": list[str]|null }`（前端从计算规则构建器取；`sample_values` 截断 ≤8 个、单值 ≤40 字符，复用 `modeling._sample_preview` 范式）。
- **校验（防幻觉）**：`aggregation ∈ {sum,avg,count_distinct,count,max,min}`；`column` 必须在 `dataset.schema_json` 真实列内；`dataset_id` 须属当前用户可见项目（B9.3）。非法 → 40000。
- **响应 data**：`{"name": str, "aliases": [str], "calc_notes": {"rationale":str,"alternatives":str,"pitfalls":str}, "llm_configured":bool}`。降级：`llm_configured=false` → 返回 `llm_configured=false` 且各字段空，前端按钮禁用并提示"未启用 AI"。**绝不自动入库**——仅回填表单，保存仍走 `POST /metrics`。

### 3.4 #4 字段语义标注（P2）  `POST /ai/dataset-semantic-annotations`（生成） + `PUT /datasets/{id}/semantic-annotations`（落库）
- **生成 `POST /ai/dataset-semantic-annotations`** Body `{ "dataset_id": int }`：对 `schema_json` 中的文本/低基数列（与关系复审口径一致），附样本值（截断），LLM 给每列 `semantic_note`；输出键必须 ∈ 真实列名，非法丢弃。响应 `{"annotations": {column: note}, "llm_configured":bool}`。降级：`llm_configured=false` → `annotations={}`（跳过标注，现状不变）。
- **落库 `PUT /datasets/{id}/semantic-annotations`** Body `{ "annotations": {column: note} }`：覆盖写 `Dataset.column_semantics_json`（整组替换；空 map 清空）。须 admin（与数据集改名同级权限）。
- 消费：返回 `dataset_to_dict` 时附带 `column_semantics`（供关系向导/指标建议后续消费；本批仅落库+展示，消费改造留作后续，不阻塞）。

### 3.5 #5 问数主动推荐升级（P2）  `GET /query/ask/suggestions`（接口不变，service 升级）
- 现有 `build_suggestions(db, user, limit, project_id)` 升级：先取项目内最近异动（`detect_for_project` 的 anomalies，Top2，或轻量最近异动查询），拼出动态推荐如"近 7 天 `{{指标名}}` 突增 X%，点击查看按 `{{维度}}` 拆解"——其中 X% 由服务端 `fmt_pct` 预格式化（无裸数字泄露、无 LLM 参与，纯规则拼装）。异动为空/失败时回退静态推荐（现状）。权限同源 `restricted_metric_ids` + `project_id`。

### 3.6 #6 归因下钻 AI 解读（P2）  `POST /ai/attribute-interpretation`
- **Body**：`{ "metric_id": int|str, "start", "end", "dimensions": [str], "compare":"mom" }`（与 `attribute-tree` 同参，但只算根节点）。
- **逻辑**：内部调用 `attribute_tree_node(path=[])` 取第一层 `children` TopN 贡献，构建 ref_table（贡献占比占位符 + 维度值名字符串），LLM 生成**一句**解读。
- **响应 data**：`{"interpretation": str|null, "source":"llm|rule", "llm_configured":bool}`。降级：失败/`llm_configured=false` → `interpretation=null`，前端不展示该句（下钻结果照常）。

---

## 4. 数据模型变更（迁移方式）

### 4.1 `Dataset` 新增列（功能 #4 落库）
- 模型（`infra/models.py`）：`column_semantics_json: Mapped[str] = mapped_column(Text, default="{}")`。
- 迁移（`infra/database.py` 的 `migrate_project_columns`，在 `datasets` 表的 PRAGMA 分支内，幂等）：
```python
if "column_semantics_json" not in cols:
    conn.exec_driver_sql(
        "ALTER TABLE datasets ADD COLUMN column_semantics_json TEXT DEFAULT '{}'"
    )
```
- 兼容性：存量库缺列自动补；新装库 ORM 直接生成；标注为空（`{}`）不影响任何现有功能（读取方缺省 `{}`）。无新表、无结构重建，零迁移风险。

---

## 5. 时序说明

### 5.1 #1 看板速览
1. 用户进入看板，前端 `fetchData` 后判断 `llm_configured`（可复用 `/llm` 状态接口或首次速览返回标记）决定是否显示"AI 速览"按钮。
2. 用户点击按钮 → `GET /ai/dashboard-summary?project_id&start&end&metric_ids`（同周期命中前端缓存则跳过请求）。
3. 后端 `build_dashboard_summary`：取可见 active 指标（封顶 15）→ 逐指标 `compute_metric_value(compare="mom")` 得当期值与环比 → 构建 ref_table（值/环比占比占位符 + 标签）→ 调 `ai_narrative.llm_narrative(timeout=60,retries=0)` → 回填后返回 sections。
4. LLM 缺配/失败 → 返回 `rule_text` 计数句（`source=none/rule`）。
5. 前端渲染速览卡；`llm_configured=false` 时按钮隐藏、仅显示规则句。

### 5.2 #2 异动假设
1. 用户打开指标详情页，前端在异动区/节点概要下方调用 `GET /ai/anomaly-hypothesis?metric_id&start&end`。
2. 后端 `detect_for_metric(metric, detect_date=end)`：非 abnormal/materal=False → `{has_anomaly:false}`。
3. 命中异动 → 对 `metric.dimensions[:2]` 各层 `attribute_delta(start,end,compare="mom",top_n=3)` 取 Top3 贡献与方向一致/背离事实 → 构建 ref_table（贡献占比占位符 + 维度名/贡献者名）→ `ai_narrative.llm_narrative` → 假设句。
4. 降级 → 返回 `fallback_action_hint`（固定 `_action_hint` 文案），前端只展示固定文案。

### 5.3 #3 口径助手
1. 用户在建指标/编辑页计算规则区点"AI 帮写口径" → `POST /ai/metric-calc-notes`（带 dataset_id/column/aggregation/alias/sample_values）。
2. 后端校验列名/聚合白名单 → 调 `ai_narrative` 之外的专用 `chat_json`（timeout=60,retries=0，结构化 JSON 输出，非叙述类，走防幻觉校验而非占位符审计）→ 返回 name/aliases/calc_notes。
3. 前端回填表单字段（用户可改），**不自动保存**；保存仍走 `POST /metrics`。

### 5.4 #4 字段标注（P2）
1. 数据集管理页点"AI 标注字段含义" → `POST /ai/dataset-semantic-annotations` 生成 → 展示预览。
2. 用户确认 → `PUT /datasets/{id}/semantic-annotations` 落库 `column_semantics_json`（覆盖）。
3. 降级（未配 LLM）→ 返回空 annotations，按钮提示"未启用 AI"或跳过。

### 5.5 #5 问数推荐（P2）
1. 用户进入问数页，前端调 `GET /query/ask/suggestions?project_id`（接口不变）。
2. 后端 `build_suggestions` 先取项目最近异动 → 拼动态推荐（数字 `fmt_pct` 预格式化）→ 与静态推荐轮转交错返回。无异动/失败 → 现状静态推荐。

### 5.6 #6 归因解读（P2）
1. 用户在详情页生成归因树（根节点）→ 前端调 `POST /ai/attribute-interpretation`（同参，只算根节点）。
2. 后端 `attribute_tree_node(path=[])` 取第一层 TopN → 构建 ref_table → `ai_narrative.llm_narrative` 生成一句解读。
3. 降级 → `interpretation=null`，前端不展示该句。

---

## 6. 有序任务列表（P1 批 1~5，P2 批 6~9）

> 依赖关系与涉及文件标注在每项末尾。

### P1 批
- **任务 1（基础·引擎）**：新建 `app/domain/ai_narrative.py`（占位符/审计/回填/通用 `llm_narrative`/格式化）；`chat_json` 加 `retries` 形参（默认 1）；`report/narrative.py` 重构为消费方（导入原语，行为与测试不变）。新增 `tests/test_ai_narrative.py`。→ 涉及：`ai_narrative.py`(新)、`infra/llm.py`(改)、`report/narrative.py`(改)、`tests/*`。
  - 依赖：无。
- **任务 2（基础·迁移）**：`Dataset` 加 `column_semantics_json`；`migrate_project_columns` 幂等 ALTER。→ 涉及：`infra/models.py`(改)、`infra/database.py`(改)。
  - 依赖：无（前置放 P1 以解耦 P2）。
- **任务 3（功能 #1）**：`app/domain/ai/dashboard_summary.py` + `api/routes/ai.py` 的 `GET /ai/dashboard-summary` + 注册路由 + 前端 `Dashboard.vue` 按钮/缓存/规则句。→ 依赖：任务 1。
  - 涉及：`ai/dashboard_summary.py`(新)、`api/routes/ai.py`(新)、`main.py`(改)、`web/views/dashboard/Dashboard.vue`(改)、`web/api/ai/index.js`(新)。
- **任务 4（功能 #2）**：`app/domain/ai/anomaly_hypothesis.py` + `ai.py` 的 `GET /ai/anomaly-hypothesis` + 前端 `MetricDetail.vue` 异动假设展示。→ 依赖：任务 1（复用 `anomaly/service.detect_for_metric`、`anomaly/attribution.attribute_delta` 现有）。
  - 涉及：`ai/anomaly_hypothesis.py`(新)、`api/routes/ai.py`(改)、`web/views/metric/MetricDetail.vue`(改)。
- **任务 5（功能 #3）**：`app/domain/ai/calc_notes.py` + `ai.py` 的 `POST /ai/metric-calc-notes`（防幻觉校验）+ 前端 `MetricAdmin.vue` "AI 帮写口径"按钮（回填不自动存）。→ 依赖：任务 1。
  - 涉及：`ai/calc_notes.py`(新)、`api/routes/ai.py`(改)、`web/views/metric/MetricAdmin.vue`(改)。

### P2 批
- **任务 6（功能 #4）**：`app/domain/ai/semantic_annotations.py`（`suggest_`/`save_`）+ `ai.py` 的 `POST /ai/dataset-semantic-annotations` 与 `PUT /datasets/{id}/semantic-annotations` + 前端 `DatasetManage.vue` 标注按钮/展示 + `dataset_to_dict` 返回 `column_semantics`。→ 依赖：任务 1、任务 2。
  - 涉及：`ai/semantic_annotations.py`(新)、`api/routes/ai.py`(改)、`api/routes/datasets.py`(改)、`web/views/dataset/DatasetManage.vue`(改)、`domain/ingestion/service.dataset_to_dict`(改)。
- **任务 7（功能 #5）**：升级 `domain/ask/service.build_suggestions`（注入最近异动动态推荐，数字预格式化，权限同源）。→ 依赖：任务 1（格式化）/现有 `ask`。接口不变，前端 `Ask.vue` 无需大改。
  - 涉及：`domain/ask/service.py`(改)。
- **任务 8（功能 #6）**：`app/domain/ai/attribute_interpretation.py` + `ai.py` 的 `POST /ai/attribute-interpretation` + 前端 `MetricDetail.vue` 根节点解读展示。→ 依赖：任务 1（复用 `anomaly/attribution.attribute_tree_node`）。
  - 涉及：`ai/attribute_interpretation.py`(新)、`api/routes/ai.py`(改)、`web/views/metric/MetricDetail.vue`(改)。
- **任务 9（回归测试）**：`tests/test_ai_features.py` 六功能集成/单测（monkeypatch LLM 注入，离线可回归）；全量回归报告/建模/问数既有测试确保无破坏。
  - 依赖：任务 1~8。

---

## 7. 共享约定（跨文件契约）

1. **算写分离原语单一来源**：`app/domain/ai_narrative.py` 的 `PLACEHOLDER_RE`/`BARE_DIGIT_RE`/`audit_sentence`/`backfill` 是占位符与审计的唯一实现；报告与新功能均从此导入，禁止在各模块私自定义副本。
2. **LLM 调用纪律**：所有新调用点 `chat_json(..., timeout=60.0, retries=0)`；叙述类必须经 `ai_narrative.llm_narrative`（含三道审计）；结构化 JSON 类（#3/#4）用 `_llm_*`（自行 `chat_json`）但须经白名单校验（aggregation ∈ {sum,avg,count_distinct,count,max,min}；列名 ∈ 真实 schema；维度/筛选值 ∈ 真实候选）。
3. **格式化一致**：数值 `fmt_metric_value`（`f"{v:,.1f}"`）↔ 前端 `formatMetricValue`；百分比 `fmt_pct`（`f"{p:.1f}%"`）↔ 前端 `formatPercent`；涨跌配色仅由前端 `TrendBadge`/`--pwc-up`/`--pwc-down` 决定。
4. **降级契约**：每个新接口响应含 `llm_configured` 布尔；叙述类额外含 `source`（`llm`/`rule`/`none`）与 `rule_text`/`fallback_action_hint`；未配置/失败均不抛 500（调用方 `try/except` 包裹 LLM 段）。
5. **项目隔离（B9.3）**：所有新接口与 service 均按 `project_id` 过滤；`restricted_metric_ids` 权限同源；禁止跨项目数据。
6. **响应包装**：新接口一律 `ok_response(data)`（或 `fail_response` 仅业务参数错 40000）。
7. **名词单一数据源**：新增/修改名词定义只在 `web/src/constants/glossary.js`，页面引用 `glossaryTerm(key)` / `<TermTip term="..."/>`，不另写文案。
8. **测试注入约定**：叙述类测试 `monkeypatch.setattr("app.domain.ai_narrative.chat_json", fake)`；结构化 JSON 类测试 `monkeypatch.setattr("app.infra.llm.chat_json", fake)`——与现有 `narrative.py` 注入方式一致，无需 mock HTTP。

---

## 8. 测试要点

- **引擎单测**（`test_ai_narrative.py`）：① 含未知 ref 的句子被 `audit_sentence` 判否；② 去占位符后含数字被判否；③ `backfill` 正确回填；④ `llm_narrative` 在 `resolve_llm_config` 返回 `None` 时返回 `None`；⑤ `chat_json` 抛异常时返回 `None` 不抛。
- **#1 看板速览**：monkeypatch LLM 返回含占位符的合法 JSON → 校验回填后无裸数字、sections 非空；LLM 返回越纲 ref → 该句被剔除、降级规则句；`llm_configured=false` → 返回 `rule_text` 且 `source=none`。
- **#2 异动假设**：非 abnormal 指标 → `has_anomaly=false`；abnormal 指标 + 合法 LLM → `hypothesis` 非空且无裸数字；失败 → `fallback_action_hint` 保留。
- **#3 口径助手**：非法 `aggregation`（如 "delete"）或不存在列 → 40000；合法 → 返回结构含三字段；`llm_configured=false` → 字段空。
- **#4 字段标注**：非法返回键（不在 schema）被丢弃；落库后 `GET /datasets/{id}` 的 `column_semantics` 与提交一致；重新标注覆盖；空 map 清空。
- **#5 问数推荐**：有异动时推荐文案含预格式化百分比（`fmt_pct`）且无裸数字；无异动时回退静态推荐。
- **#6 归因解读**：根节点合法 LLM → `interpretation` 单行非空无裸数字；失败 → `null`。
- **回归**：既有 `test_narrative.py`/`test_ask*`/`test_modeling*`/`test_query*` 全绿，确认报告叙述、问数意图、建模建议、计算出口行为不变；`migrate_project_columns` 对新旧库均幂等。
