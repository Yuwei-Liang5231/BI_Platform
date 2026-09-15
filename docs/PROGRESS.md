# 开发进度（PROGRESS.md）

> 配套文档：**《AI原生BI平台_开发操作手册.md》**（常用命令、环境路径、报错处理、接口契约、问答沉淀）——操作类问题先查它。

> 维护规则：每批次交付后更新本文件。任何新会话先读本文件再继续，不依赖对话记忆。
> 最后更新：2026-09-10

## 一、批次状态

| 批次 | 内容 | 状态 | 完成日期 | 关键文件 | 自测结果 |
|---|---|---|---|---|---|
| **B0** | 工程脚手架与地基 | ✅ 完成 | 2026-09-10 | `bi-platform/backend/app/`（core/config·response·logging、infra/database·repository·storage·models、api/routes/health、main.py）、`tests/`（6 个测试文件）、`env/.env.{dev,test,uat,pro}`、`scripts/bi.{sh,ps1}` | **pytest 18 passed**；uvicorn 冒烟 `GET /api/health` → HTTP 200，`code=0`，`env=dev`，`database=up`；dev 库建表验证通过（12KB，含 key_value）；`GET /` 重定向 `/docs` |
| **S1** | 多行业合成数据（D21） | ✅ 完成 | 2026-09-10 | `bi-platform/scripts/gen_synthetic_data.py`、`bi-platform/data/synthetic/{dataset_a,dataset_b,manifest.json}` | 两套各 10 万行生成完毕；编码校验（orders/accounts=GBK，其余 UTF-8）通过；混杂类型列与异常值抽查通过；**backend/app 行业词 grep = 0 命中** |
| **B1** | 数据接入（阶段 1 最小版，D12/D13） | ✅ 完成 | 2026-09-10 | `app/domain/ingestion/{encoding,types,service}.py`、`app/api/routes/datasets.py`、`app/infra/models.py`（Dataset/DatasetRelation/DatasetCoverage 三表）、`tests/{test_encoding,test_types,test_ingest_api}.py` | **pytest 56 passed**；合成数据 8 文件端到端通过；真实数据 2 份验收通过（BKG_GHOSTt.xlsx + SDC Hours CSV 41,922×26）并推动 3 轮修复：①datetime/NULL 推断 ②编码采样误判自动重试（GBK→GB18030）③斜杠日期 `2025/9/30` 识别；另强化 500 响应携带异常详情、int64 溢出与文件占用转 400、删除级联文件清理不静默 |
| **B2** | 指标元数据 + 编译器 | ✅ 完成 | 2026-09-10 | `app/domain/metric/{schema,compiler,service}.py`、`app/api/routes/metrics.py`、`app/infra/models.py`（Metric/MetricChange/MetricSqlArchive 三表）、`tests/{test_compiler_golden,test_compiler_reject,test_compiler_exec,test_metrics_api}.py`、新增依赖 duckdb 1.5.5 | **pytest 117 passed**（B2 新增 61）；golden SQL 22 条（A 12 + B 10）逐字比对；DuckDB 真实执行验证 7 条（数字与手工 SQL 一致、除零返回 NULL）；拒绝用例 16 条（超纲算子/OR/缺表关系/列不存在/多日期列歧义等）；API 端到端 14 条（创建/别名搜索/口径变更原子生效/软删除）；dev 库真实冒烟：创建 gmv_paid/mrr_active/avg_ticket 三指标 + 口径变更 ver1→2 + 留痕 + 存档同步；**backend/app 行业词 grep = 0 命中** |
| **B3** | 统一计算服务 query | ✅ 完成 | 2026-09-10 | `app/domain/query/service.py`、`app/infra/{cache,duckdb_session}.py`、`app/api/routes/query.py`、`tests/{test_query_api,test_cache}.py` | **pytest 139 passed**（B3 新增 22：API 集成 19 + LRU 单元 3... 含缓存失效/周期完整性/导出）；dev 冒烟：gmv_paid 2025 全年 7.16 亿（ver3 中文状态口径）、客单价 32,248.71、MRR 同比无前期数据正确返回 null、CSV 导出带 BOM；改口径 → 缓存失效 → 新值生效全链路验证 |
| **B4** | auth 与权限双出口 | ✅ 完成 | 2026-09-10 | `app/domain/auth/{security,service}.py`、`app/api/routes/auth.py`、`app/api/deps.py`（CurrentUser/AdminUser/WriterUser）、`app/infra/models.py`（User/MetricVisibilityRestriction 两表）、`tests/{test_auth_api,test_visibility}.py`、新增依赖 PyJWT 2.13 | **pytest 160 passed**（B4 新增 21：认证 9 + 用户管理与角色边界 8 + 可见性双出口 7... 实为 auth 9 + visibility 7 + 用户管理 5）；dev 冒烟 12 步全对：401/40100 无 token、admin 登录、真实取数、建 viewer、role 登记后 viewer 目录隐藏 + 取数/导出 403、admin 不受影响、清空恢复；**backend/app 行业词 grep = 0 命中** |
| **B5** | 行业指标模板库 | ✅ 完成 | 2026-09-10 | `bi-platform/templates/seed/{ecommerce,saas,restaurant,general}.yaml`（101 条）、`app/domain/templates/{loader,service}.py`、`app/api/routes/templates.py`、`backend/scripts/smoke_b5.py`、新增依赖 pyyaml 6.0.3 | **pytest 176 passed**（B5 新增 16）；幂等导入 / pending→数据上传→revalidate 升级 / requires=extended-operator 登记 disabled 全链路测试通过；顺手修复 B2 遗留缺陷：跨表过滤列 JOIN 的维度表未纳入 `dataset_names`（执行时缺视图必崩）；**backend/app 行业词 grep = 0 命中**（YAML 模板在 app 外，模板机制行业无关） |
| B6 | 前端主体（PwC 品牌） | ✅ 完成 | 2026-09-10 | `web/src/`（Vue 3.5 + Vite 8 + Element Plus 2.14 + ECharts 6：六页面/布局/api 层/stores/router/styles）、`web/vite.config.js`（preview.proxy B7 补） | 四环境构建通过；真实浏览器走查 6 项 PASS（console 0 错误）；交付后 4 轮反馈修复：TrendBadge「—」、exportCsvBlob 拆分（点卡片不再下载）、isoDate 本地时区、三模式计算规则编辑器、口径分歧归一化展示 + 编辑区 + 顺带修复编辑 400 bug（stable 深比较 + reason 按需） |
| B7 | MVP 联调与验收 | ✅ 完成 | 2026-09-11 | `backend/scripts/smoke_b7.py`（28 步端到端）、`web/scripts/smoke_b7_web.cjs`（9 步联调走查）、`AI原生BI平台_B7验收报告.md` | **smoke_b7 28 passed**（GBK 上传→关系→试编译→取数 7.156 亿与 B5 印证→缓存 hit→环比→CSV BOM→周期完整性→改口径 ver2 缓存 miss 全局生效→超纲拒绝→影响清单→权限双出口→清理）；**pytest 176 passed 无回归**；**浏览器走查 9 passed**（页面值与 API 逐字一致、变更历史留痕、console 0 错误）；**发现并修复缺陷：MetricDetail 变更历史不加载（loadAll 缺 fetchChanges）** |
| B7-fix | 验收反馈修复 4 轮 + 重导入死锁 | ✅ 完成 | 2026-09-11 | MetricAdmin/DatasetManage/request/datasets api/stores 修复；`web/scripts/smoke_{upload_fix,upload_lock,relation_type,import_result,reimport_upgrade}.cjs` 五个走查脚本 | ①上传误报（axios 30s 超时→upload 单独 600s + 状态提示优化）②上传全程锁（100% 后禁用防重复提交）③关系类型前后端枚举对齐（n:1→many_to_one）④导入结果空白（读 totals + 状态过滤默认全部）；⑤**重导入死锁（用户实测「进入不了导入结果」）**：已导入 pending 指标 checkbox 全禁用 + 导入按钮 `!checkedCodes.length` 禁死，与「重新导入即可自动启用」提示自相矛盾——修复：pending 指标可勾选且默认勾选、doImport 携带 `revalidate:true`、doImport 加错误兜底、全部已导入时空态提示；**smoke_reimport_upgrade 11 passed**（全新导入 21→pending 19 可重导→上传数据集后重导升级 19 全启用→终态空态提示，console 0 错误） |
| B7-fix2 | 自定义统计周期 + 覆盖区间透明化 | ✅ 完成 | 2026-09-11 | `web/src/composables/usePeriodRange.js`（公共组合式函数）、Dashboard/MetricCatalog/MetricDetail 三页日期区间选择器、`web/scripts/smoke_period_range.cjs` | 默认仍为上一完整自然月（保证 period_complete 与环比口径稳定）；三页支持自定义区间（el-date-picker daterange，变更即重载）；**「—」困惑根治**：卡片区间外显示「区间无数据」徽标（tooltip 展示数据覆盖范围），图表空态显示「所选区间无数据：数据覆盖为 X~Y」（利用 metric-value 响应已有的 coverage 字段）；**smoke_period_range 6 passed**（默认区间徽标→覆盖提示→切到覆盖内区间出值 4,000.50 与夹具数据吻合→目录/详情同步生效，console 0 错误） |
| B7-fix3 | 看板切主题图表残留修复 | ✅ 完成 | 2026-09-11 | `web/src/views/dashboard/Dashboard.vue`、`web/scripts/smoke_topic_switch.cjs` | **缺陷**：切主题时 selectedId 不重置且 selected 从全量指标里找 → 折线图停留上一主题指标（用户：全部主题看 GMV 后进 test 主题图表仍是 GMV）。**修复**：①selected 改为只在当前主题可见列表中找；②watch(activeTopic) 切主题自动选中该主题第一个指标（空主题清空选中）。**smoke_topic_switch 5 passed**（默认首卡跟随→手动点选跟随→切主题重置首卡→切回全部主题重置→console 0 错误） |
| B7-fix4 | 折线图「数据截至」截止线 | ✅ 完成 | 2026-09-11 | Dashboard/MetricDetail 图表 markLine、`web/scripts/smoke_cutoff_mark.cjs` | **答疑落地**：卡片「区间无数据」与折线有数据不矛盾——卡片按冻结契约要求周期被数据完整覆盖（如 subscriptions 覆盖仅到 2026-08-10，查 2026-08 整月 → 拒绝给值显示—）；折线逐日绘制、有数据的天就画。**改进**：折线尾部无数据段自动画「数据截至 MM-DD」虚线截止线（ECharts markLine，setOption 合并模式下无标记时显式清空防残留），与卡片徽标互相印证；详情页空态已带覆盖范围提示。**smoke_cutoff_mark 5 passed**（区间 09-01~09-30 数据仅到 09-03：卡片徽标+截止线渲染正确，console 0 错误） |
| B7-fix5 | **契约 v2**：部分周期真实值 + 图表消失 BUG 修复 | ✅ 完成 | 2026-09-11 | `backend/app/domain/query/service.py`、`routes/query.py`、Dashboard/MetricDetail、`web/scripts/smoke_contract_v2.cjs` | **用户确认的语义修订**：卡片「区间无数据」与折线有数据自相矛盾——覆盖与区间相交即返回真实值 + 「数据截至」标注（部分周期不虚报也不隐瞒）；环比基期对齐数据截止日（08-01~08-10 对 07-01~07-10）；缓存键纳入覆盖端点（补录数据自然失效）；**BUG 修复**：图表容器 v-if 重建后旧 ECharts 实例失联致折线消失（切无数据指标再切回），renderChart 检测 `getDom()!==chartEl` 即 dispose 重建；**pytest 177 passed**（含契约 v2 新用例：部分值 450/缓存命中/基期对齐/无交集 null）；**smoke_contract_v2 10 passed** + cutoff/period/topic 三脚本回归全过，console 0 错误 |
| B7-fix6 | **datetime 边界截断**（卡片有值折线消失）+ 前端取数竞态守卫 | ✅ 完成 | 2026-09-11 | `backend/app/domain/metric/compiler.py`、`backend/tests/{test_query_api,test_compiler_golden}.py`、Dashboard/MetricDetail | **用户报「更严重问题」**：test_sum 卡片 1,442,025 有值但折线图消失；usage 主题卡片全「区间无数据」折线却有数。**根因 1（实锤）**：时间过滤曾是 `col <= __end__`，`WriteTime` 为 **datetime** 列时被解释为 `<= 端点日 00:00:00`，端点日全部日间记录被截——逐日序列 61 天只剩 5 个 00:00:00 孤立点（`symbol:none` + 不连空值 → 线不可见），而卡片整段聚合有值；修复：编译器统一改半开区间 `>= __start__ AND < __end__+1d`（date 列语义等价），修复后 61/61 天有值且**逐日求和=卡片值=1,453,279 完全一致**（旧卡片值 1,442,025 正是少了 12-31 日间 11,254）。**根因 2（隐患）**：loadCards/loadTrend 无竞态守卫，快速切换时旧响应后到覆盖新状态（"每张卡片显示同一条旧折线"）——请求序号守卫，过期响应丢弃。**附带修复**：环比徽标恒"—"（前端取 `change`，后端实际为 `compare.change_pct` 百分数 → 除 100 映射）；MetricDetail CSV 解析丢无值行（`if(date&&value)`→`if(date)`）。**pytest 181 passed**（新增 datetime 边界 4 用例）；smoke_contract_v2 10/10 + topic 5/5 + cutoff 6/6；截图2 的卡片 null 为时点性状态（页面跨后端重启未刷新），实时复现已正常 |
| B8 | **阶段 2：完整质检 + 增量导入 + 覆盖刷新与缓存失效** | ✅ 完成 | 2026-09-14 | `app/domain/ingestion/{service,quality}.py`、`app/api/routes/datasets.py`（+quality/+data）、`web/src/views/dataset/DatasetManage.vue`、`web/src/api/datasets/index.js`、`tests/test_ingest_b8.py`（13 用例）、`web/scripts/smoke_quality_incremental.cjs`（9 步） | **pytest 207 passed**（B8 新增 13：质检 3 + 增量 6 + 缓存失效端到端 1 + 存量迁移 1 + …）；**smoke 9/9**（上传→质检→追加（列序不同+混杂值）→ver/行数/覆盖刷新→报告自动弹出→console 0 错误）；回归 smoke_contract_v2 10/10 + cutoff 6/6 + topic_race 全过。落地内容：① `GET /datasets/{id}/quality` 质检报告（逐列空值/空列/常量列/类型混杂/内容混杂 + 行级偏离定位 + 覆盖区间，按需现算不落库）；② `POST /datasets/{id}/data`（append/replace，admin）：表头列名校验（集合一致顺序可不同）→ 按现有类型转换 → **分片 Parquet 目录布局**（`data/parquet/{name}/part-*.parquet`，存量单文件首次导入自动迁移，临时分片先行、失败零副作用）→ dataset_ver+1（同事务）→ 覆盖区间重算 → 缓存自然失效（端到端验证 append 后同区间 cache=miss 且值更新）；③ 前端：质检报告对话框（问题徽标+展开行级明细）、导入数据对话框（append/replace + 覆盖二次确认 + 导入后自动弹报告） |
| B9 | **阶段 2：AI 问数 Agent（意图解析 + 理解卡 + 口径同源执行）** | ✅ 完成 | 2026-09-14 | `app/infra/llm.py`、`app/domain/ask/{__init__,service}.py`、`app/api/routes/query.py`（+ask/+ask/execute）、`tests/test_ask_api.py`（7 用例）+ `tests/ask_testset.json`（6 案例）、`web/src/views/ask/Ask.vue`、router/DefaultLayout/api 注册、`web/scripts/smoke_ask.cjs`（8 步） | **pytest 214 passed**（B9 新增 7）；**smoke 8/8**（理解卡命中→区间→执行 1,000 与数据截至→算不了提示→歧义候选黄条→console 0 错误）。落地内容：① 意图解析双通道：LLM（OpenAI 兼容 `infra/llm.py`，llm_* 配置，未配置/失败自动降级）+ **确定性关键词解析器**（显式日期/年月/上月/本月/今年/去年/最近N天/环比同比/指标 code·名称·别名匹配）——测试集离线回归 100%；② 理解卡：指标/时间/对比全部可改，多候选黄提示（最长匹配为默认），disambiguation 默认算法引用，"算不了"明确原因；③ 权限继承：候选仅含可见指标（restricted_metric_ids 同源），受限指标提示"该指标无权限"且不泄露名称，执行 403；④ 口径同源铁律：execute 走 compute_metric_value 单点出口（测试证实 execute 后看板同参请求 cache=hit 同键命中）；⑤ 前端 /ask 页：示例问题、理解卡编辑、确认计算、与看板同口径重算按钮、结果卡（涨跌箭头+文字、数据截至标注） |

## 二、已冻结的接口契约

| 契约 | 内容 | 冻结于 |
|---|---|---|
| 响应体 | `{code, message, data}`；HTTP 码与 body code 前三位对齐：0/200、40000/400、40100/401、40300/403、40400/404、40900/409、50000/500；未登记码回落 500 | B0 |
| 健康检查 | `GET /api/health` → `data: {status, app, version, env, database, time}`；db 异常 → 500/50000 | B0 |
| 业务异常 | `BusinessError(message, code, data=None)`，data 可携带结构化载荷（如 ragged_rows），全局处理器转统一响应 | B1 |
| 仓储 | `Repository(model, session)`：get/list/count/add/add_all/update/delete；元数据读写一律走此层 | B0 |
| 配置 | `APP_ENV` 进程变量选 `backend/env/.env.{env}`；进程变量 > env 文件；路径派生：`data_dir/uploads|parquet|metadata.db` | B0 |
| 数据接入 | `POST /api/datasets/upload`（multipart: file + 可选 name）→ 详情；重名自动 `_2`；列数不一致 400+行号；`GET /api/datasets`、`GET /{id}`、`PATCH /{id}/name`（改名，仅元数据标签）、`DELETE /{id}`（级联）、`GET /{id}/preview`、`GET /{id}/columns/{col}/anomalies`、`POST/GET/DELETE /{id}/relations` | B1 |
| 类型语义 | 列类型 ∈ int/float/date/datetime/bool/string/mixed；**int+float 混合 → float（非混杂）**；**date+datetime 混合 → datetime**；前导零数字 → string；**空串与字面量 `NULL`（大小写不敏感）→ null**（`N/A`/`NA` 可能是业务码，保留为 string）；date/datetime 列自动登记 `dataset_coverage`；mixed 存储 string 并标记 | B1 |
| 编码语义 | 采样识别（头 64KB）仅作初判；**流式解码失败时自动按 GBK → GB18030 依次整文件重试**，成功则记录实际编码并打 warning 日志；全部失败 → 400 并给 byte_position | B1 |
| 日期语义 | date/datetime 支持 ISO（`2025-09-30`）与斜杠格式（`2025/9/30`、`2025-9-30`），datetime 含 `HH:MM[:SS[.ffffff]]`；覆盖区间自动登记 | B1 |
| 指标 CRUD | `POST /api/metrics`（编译不过保存即拒绝）、`GET /api/metrics`（search 命中名称/code/别名、topic、status=active\|disabled\|all）、`GET /{id}`、`PATCH /{id}`（改 calc_rule 必须 reason，禁止 PATCH 置 deleted）、`DELETE /{id}`（软删除）、`GET /{id}/sql`（当前版存档）、`GET /{id}/changes`（留痕）、`POST /api/metrics/compile`（试编译不落库）；请求体 `extra=forbid` | B2 |
| calc_rule 语义 | 两种形态：扁平 `base_aggregation + source{table,column,filter}`；比率 `expression + operands{name:{table,column,aggregation,filter}}`。聚合 ∈ sum/count/count_distinct/avg/max/min（sum/avg 限数值列；count 可省 column = COUNT(*)）；filter 仅合取范式（AND 连接，`字段 op 字面量`、`IS [NOT] NULL`，字面量：数字/'字符串'/true/false）；**OR/NOT/函数/嵌套聚合/group_by/cohort/窗口一律保存即拒绝**；请求体未知字段即拒绝 | B2 |
| 编译语义 | `compile(calc_rule, datasets, relations) -> CompiledQuery`（纯函数，接口已抽象）；输出 DuckDB 方言参数化 SQL（时间范围绑定 `$__start__`/`$__end__`）+ 周期元信息（time_fields/coverage 区间并集/grain=day）；跨表过滤列只走 `dataset_relations` 显式一跳星型关系（歧义/缺失即拒绝）；除法包 NULLIF（分母 0 → NULL）；表达式操作数名不得与数据集名/保留字冲突；**口径变更 → 重编译 → 存档 → ver+1 → 留痕在同一事务原子完成** | B2 |
| time_field 语义 | operand 级 `time_field` 严格指定（跨表比率中各表日期列名不同时用）；规则级：扁平形态严格必须命中本数据集，表达式形态作为默认值（数据集有该列才应用，否则自动解析唯一日期列）；数据集有多个日期列且未显式指定 → 编译拒绝 | B2 |
| 统一取数 | `POST /api/query/metric-value`（body: metric=id或code / start / end / compare=none·mom·yoy）→ `{metric_id, metric_code, name, ver, start, end, value, period_complete, data_through, cache, coverage, grain, compare}`；`POST /api/query/export`（同参数）→ CSV 文件流（**契约例外：不走 JSON envelope**，UTF-8 BOM，Content-Disposition 文件名 `{code}_{start}_{end}.csv`）；metric 入参兼容 id 与 code | B3 |
| 周期完整性语义（**v2，2026-09-11 修订，废止 B3 旧约"不完整显示—"**） | 覆盖区间与请求区间**相交即返回真实值**（部分周期不虚报也不隐瞒）；周期未被完整覆盖 → `period_complete=false` + `data_through=`数据实际截止日（前端标注"数据截至"，环比基期对齐到该日，避免"10天 vs 全月"假跌幅）；仅覆盖未知/无交集或聚合为空 → `value=null`（前端显示"区间无数据"） | B7-fix5 |
| 缓存语义 | 进程内 LRU（512 条，线程安全，deepcopy 隔离）；键 = `metric:指标id:ver:数据集名:dataset_ver(有序):start:end:sha1(sql)[:12]:覆盖端点`（v2 起覆盖端点参与键，部分周期值可安全入缓存，数据补录 → 键变化自然失效）；口径变更（ver+1）或数据重导入（dataset_ver+1）→ 旧键自然失效；多 worker 部署时仅需替换 `app/infra/cache.py` 实现 | B3 |
| 环比/同比 | `compare=mom`（紧邻上一等长周期）/ `yoy`（去年同起止，2/29 回退 2/28）；上期经同一编译产物计算（与本期同一条 SQL），`change_pct = (cur-prev)/abs(prev)*100` 保留 4 位；上期 None 或 0 → change_pct=null | B3 |
| 执行语义 | 取数时**实时重编译**（不执行存档 SQL——存档仅用于展示与历史回看），保证使用当前覆盖区间与表关系；DuckDB 每次计算新建内存连接（线程安全 + 规避写并发），视图名 = 数据集名，parquet 路径字面量 + 单引号转义（CREATE VIEW 不支持 prepared parameter） | B3 |
| 认证语义 | `POST /api/auth/login`（公开）→ `{token, token_type, user}`；除 health/登录/文档外**所有接口需 JWT**（`Authorization: Bearer <token>`）；无效/过期 → 401/40100；账号停用 → 403/40300（已签发 token 立即失效，每请求回库校验）；口令 pbkdf2_sha256 12 万轮；登录失败统一文案（防用户名枚举）；启动时 users 为空自动建引导 admin（`bootstrap_admin_username/password` 配置，dev 默认 admin/admin123） | B4 |
| 角色权限 | admin（全部权限：账号、数据集上传/删除/关系、可见性登记）/ analyst（指标建改删 + 全部读）/ viewer（只读）；依赖名 `CurrentUser`/`AdminUser`/`WriterUser`；**数据集写操作 admin 专用（D12）**，指标写操作 analyst 即可 | B4 |
| 权限双出口（不变式 2） | 可见性模型 = 默认可见 + `metric_visibility_restriction` 负向登记（subject_type=role/department）；**同一套判定函数 `restricted_metric_ids` 同时用于两出口**：query 数值出口（metric-value/export → 403 拒算）与 metric 元数据出口（目录/搜索过滤、详情/SQL/历史 → 403），受限指标的名称与口径不泄露；admin 恒全量可见；登记管理 `GET/PUT /api/auth/metrics/{id}/restrictions`（PUT 整组替换、items=[] 清空，幂等） | B4 |
| 模板库（B5） | 位置：`bi-platform/templates/seed/*.yaml`（**数据而非代码**，维持 app 行业词 grep=0；目录由 `templates_dir` 配置，默认 `<repo>/templates/seed`）。API：`GET /api/templates/industries`（列表+指标数）、`GET /api/templates/{industry}`（明细+每条的 `imported`/`imported_status`）、`POST /api/templates/import`（body `{industries?, codes?, revalidate?}`，WriterUser，请求体 extra=forbid）。模板条目字段：code/name/aliases/definition/topic/parent（同包先出现）/dimensions/disambiguation/requires/calc_rule；包内 code 唯一、parent 须先出现、calc_rule 过 `parse_calc_rule` 结构校验。**导入状态判定**：requires=extended-operator → `disabled`（口径已登记、暂不可计算）；数据集齐备且编译通过 → `active`+写编译存档；缺数据集/编译未过 → `pending`（待绑定数据集）。**幂等**：同 code 已存在即 skipped（不覆盖人工修改）；`revalidate=true` 时 pending 指标编译通过即升级 active。`pending` 指标不在默认目录（status=active 过滤）且取数 400「尚未绑定数据集」；ratio 类指标 value 为原始比率（×100 是前端职责） | B5 |
| 质检报告（B8） | `GET /api/datasets/{id}/quality`（登录即可）：`{dataset_id, name, dataset_ver, row_count, column_count, columns:[{name, type, mixed, null_count, null_ratio, distinct_count, is_empty, is_constant, issues:[{level:high\|medium\|info, code, message}], dominant_type?, type_counts?, deviations?(行号+原始内容+实际类型, 前20), deviation_total?, null_sample_rows?(非字符串列前10), null_total?}], coverage:[…]}`。issue code：empty_column(high)/mixed_types(high, 上传时混杂)/mixed_content(high, 字符串列内容形态不齐——增量导入后 schema 仍 string 但内容混杂同样暴露)/high_null_ratio(medium, >50%)/constant_column(info)。**按需现算不落库**，增量导入后报告自然反映最新数据 | B8 |
| 增量导入（B8） | `POST /api/datasets/{id}/data`（multipart: file + mode=append\|replace，默认 append；admin 专用）→ 数据集详情 + `import:{mode, rows_added, dataset_ver, row_count, part_file, audit_file}`。**校验**：表头列名集合必须与现有 schema 完全一致（顺序可不同，按现有 schema 重排；不符 400 + missing/extra 列表）；新行按现有列类型转换（失败 400 带行号列名）；ragged 行 400。**落盘**：分片 Parquet 目录布局 `data/parquet/{name}/part-*.parquet`（新上传数据集默认此布局；存量单文件首次导入自动迁移为 part-0001）；先写临时分片成功后再动既有数据（失败零副作用）；原始件留档 `uploads/{name}__{mode}__{ts}{ext}`。**元数据**：row_count 累加（append）/替换（replace）、覆盖区间整体重算、dataset_ver+1 与之**同事务** → 指标缓存键含 dataset_ver 自然失效（append 后同区间取数 cache=miss 且值更新，已端到端验证）。replace 不做在线单元格编辑——数据修正走「改源文件 → replace 重导」闭环 | B8 |
| 问数（B9） | `POST /api/query/ask`（body {question}，CurrentUser）→ 理解卡 `{question, llm_configured, source: llm\|fallback, metric:{id,code,name}\|null, start, end, compare, time_is_explicit, ambiguous:[{field, options, default, reason}], disambiguation_note, can_compute, no_metric_reason}`；`POST /api/query/ask/execute`（body {metric, start, end, compare}）→ 与 metric-value 完全同构（同一 compute_metric_value 出口）。**铁律**：LLM 只产意图（候选清单内选指标/合法日期/枚举 compare），数值由指标中心统一计算；候选仅含当前用户可见 active 指标（权限继承，与看板同一 restricted_metric_ids）；无匹配 → can_compute=false + no_metric_reason（受限指标提示"该指标无权限"不泄露名称）；无显式时间默认上一完整自然月。LLM 配置（llm_base_url/api_key/model）未配置时 source=fallback（确定性关键词解析器），测试集 `tests/ask_testset.json` 离线回归 100% | B9 |

## 三、技术债务与备忘

| 项 | 说明 |
|---|---|
| ~~时间戳存 UTC~~（**已修** 2026-09-10） | B0-B3 所有 `created_at/updated_at/compiled_at` 用 `utc_now()` 入库，SQLite 存 naive 值导致人工核对慢 8 小时（梁老师发现）。修复：models.py 改为 `local_now()`（系统本地时间）；dev 库存量 42 列已批量 +8h 校正。冻结约定：**元数据时间一律本地时间入库**（`/api/health` 的 `time` 字段仍为 UTC ISO 格式，带 +00:00 后缀，不受影响） |
| httpx 弃用警告 | 测试输出 `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2`。当前不影响运行，属前瞻兼容提示；待 Starlette 正式移除 httpx 支持时再迁移 `httpx2` |
| Swagger 占位符陷阱 | Swagger 表单中 `name` 等字符串字段的默认占位值是 `string`，直接点 Execute 会把它当真实值传入（梁老师真实踩到：数据集被命名为 "string"）。修复后可用 `PATCH /api/datasets/{id}/name` 改名，无需重传 |
| 500 排查约定（B1 强化） | 未处理异常的响应体 `data` 会携带 `{exception, detail}`（截断 500 字符），**遇到 500 直接看响应体即可定位**，完整堆栈仍在服务端日志 |
| 删除级联文件清理 | `DELETE /api/datasets/{id}` 响应含 `files_removed` / `files_failed`；文件被占用导致删除失败时记入 files_failed 并打日志（不再静默），元数据仍会删除（孤儿文件需手动清，路径在 files_failed 里） |
| ~~建表静默失败~~（**已修**） | `create_all()` 未导入模型模块 → `Base.metadata` 为空 → 建表静默失败（db 0 字节但 SELECT 1 仍通过）。修复：create_all 内 `import app.infra.models`；新增回归测试 `tests/test_database.py`；新增模型模块需在 create_all 登记导入 |
| `--reload` 热重载不可靠 | 修改 domain 层代码后 uvicorn `--reload` 曾未生效（旧进程继续服务，导致验收数据用旧逻辑）。约定：**改代码后重启服务再验收**，勿依赖热重载结果 |
| 上传文件句柄 | 上传走 NamedTemporaryFile + 两遍流式扫描；scan_rows 提前退出时显式 close 迭代器（Windows 句柄泄漏会锁临时文件）。后续新增扫描逻辑须保持该模式 |
| ~~secret_key 占位~~（**已补** B4） | dev 默认值已加长至满足 HMAC-SHA256 最低 32 字节（消除 InsecureKeyLengthWarning）；**pro 环境部署前必须在 `.env.pro` 覆盖为真实随机密钥**，同时覆盖 `bootstrap_admin_password` |
| 测试访问私有 `_session_factory` | test_repository.py 直取了 session 工厂，B1 引入 get_db 依赖后改为规范注入 |
| ~~uvicorn/pytest 已入 venv~~（**已补**） | `backend/requirements.lock` 已生成（pip freeze，35 项含版本）；精确装依赖时用 `pip install -r requirements.lock`，日常开发用 `requirements.txt`（宽松范围） |
| 合成数据规模 | 当前 10 万行/事实表；B1 联调前可再生成 100 万行压测（`--rows 1000000`，实测可承受） |
| 订阅"cohort/留存"语义（**已验证**） | B2 验收结论：cohort/分组/窗口等结构外语义在 calc_rule 校验层**保存即拒绝**并明确提示"属于阶段 4a 扩展算子"（test_compiler_reject 验证），不会静默算错 |
| 表达式 CTE 名冲突 | 表达式形态的操作数名会变成 SQL CTE 名：与数据集同名或用 `__start__`/`__end__` 保留字都会被拒绝（避免 CTE 与表名歧义） |
| 除法语义 | 比率类除法统一包 `NULLIF(分母, 0)`：分母为 0（如空周期）返回 NULL 而非报错；B3 前端按"不完整周期显示 —"承接 |
| SQL 占位参数 | 编译产物用 DuckDB 原生命名参数 `$__start__`/`$__end__`，B3 执行时以 `con.execute(sql, {"__start__": d1, "__end__": d2})` 绑定（test_compiler_exec 已预演） |
| 8100 旧进程 | ~~B2 交付时 8100 端口仍是 B1 旧代码进程~~ B3 冒烟在 8102 完成；**梁老师重启 8100 后即同时获得 B2+B3 能力**（Ctrl+C 后 `.\scripts\bi.ps1 run -Env dev -Port 8100`） |
| ~~权限过滤占位~~（**已落地** B4） | query service `user` 参数已变为必填并强制可见性判定；metric 元数据接口同步过滤。行级权限仍留阶段 4b（B15） |
| dataset_ver 无递增路径 | B1 无"覆盖重导入"能力（重传同名文件自动后缀为新数据集），故 dataset_ver 恒为 1；缓存键已纳入该因子（test_query_api 验证递增即失效），B8 增量导入落地时在导入事务内递增即可 |
| 导出为逐日循环执行 | `export`/序列按天循环调用 `_compute_range`（复用缓存），单日查询毫秒级；10 万行×365 天实测秒级。数据量上来后（B8+）可换 DuckDB 窗口聚合一次算完，接口不变 |
| 示例指标状态值口径 | dev 库 3 个示例指标的 filter 已修正为中文状态值（合成数据 orders.order_status = 已支付/已退款/部分退款；subscriptions.status = active/churned）。曾因英文 'paid' 匹配 0 行导致客单价返回 null——**查数为 null 时先核对 filter 字面量与实际数据值**（`GET /api/datasets/{id}/columns/{col}/anomalies` 可看枚举分布） |
| ~~跨表过滤编译缺陷~~（**已修** B5） | 编译器对"跨表过滤列"生成的 SQL 会 JOIN 维度表，但该维度表此前未纳入 `CompiledQuery.dataset_names` → 执行时不建视图必崩（B2 golden 只断言 SQL 文本、未端到端执行该路径，故漏网）。修复：`_operand_subquery` 返回 joined 表名，`track_joined` 纳入 dataset_names（无时间绑定，参与缓存键） |
| 模板表名绑定约定（B5） | 模板 calc_rule 的 `table` 必须与**数据集名**（上传文件名 stem）一致；测试库注意与 `test_ingest_api` 的命名隔离（该测试已改为显式 `ing_orders`/`ing_users`，防与模板表名撞名） |
| pending 指标升级路径（B5） | 数据上传后调 `POST /api/templates/import` + `revalidate=true` 即自动升级 active（补写编译存档），无需人工 PATCH；幂等导入对已 active/disabled 指标永远 skipped，不覆盖人工修改 |
| WorkBuddy 守卫误伤文件删除（B7，环境特有） | WorkBuddy 托管 python 进程内 `Path.unlink` 被其安全删除 shim（sitecustomize）拦截，「删除数据集」时文件清理可能 500——**DB 元数据已删，仅孤儿文件需手动清**（uploads/parquet 下同名文件）。梁老师本机自起的服务进程不含 shim，不受影响 |
| 删除指标/数据集后前端残留选中态（B7） | 看板/详情仍停留已删指标时取数 400（数据行为正确）。B8+ 可在删除成功后联动清理前端选中状态（localStorage/pinia） |
| 本机端口监听限制（B7） | 5175/5180 等端口被本机安全策略拒绝（EACCES，非 Windows 排除段）；联调 preview 用 5183。`vite.config.js` 已补 `preview.proxy`（`/api` → `BI_BACKEND_ORIGIN` 环境变量或默认 8100） |
| 软删指标 code 永久占用唯一性（B7） | `create_metric` 的 code 唯一性校验 `repo.count(code=code)` 不过滤 status——软删指标的 code 不可复用（保守设计：防审计口径混淆）。smoke_b7.py 已做幂等自愈：启动清理残留 + code 被占自动换时间戳后缀 + 异常优雅退出；重跑不再撞 409 |
| ~~上传 30s 超时误报 + 对话框状态脱节~~（**已修** 2026-09-11） | ① axios 全局 timeout=30s：大文件（59.5 万行实测）解析超 30s 前端误报「服务异常」，但后端实际继续处理**成功入库**——诊断「上传失败」先查库。修复：upload 请求单独 timeout=10 分钟；超时提示改为「可能仍在处理，稍后刷新核实」；500 透出后端 data.detail（B1 约定）。② el-upload 未绑 file-list 导致重开后「看似已选实则未选」的误导校验。修复：v-model:file-list 单一事实来源 + 打开/成功后全量重置 + 100% 显示「服务器解析中」。③ 顺带修 stores/dataset.js `fetchDetail` 引用未定义 detail ref 的隐藏雷。走查 smoke_upload_fix.cjs 6/6 PASS |
| ~~表关系登记 relation_type 前后端枚举不一致~~（**已修** 2026-09-11，用户验收发现） | B6 前端下拉发 `1:n`/`n:1`，后端契约是 `one_to_many`/`many_to_one` 等（datasets.py VALID_RELATION_TYPES），保存必 400。修复：前端值为后端枚举、标签用业务符号+中文说明（如 `n:1（左表多条对右表一条）`），默认 many_to_one；关系列表类型列同步映射显示。另加「上传提交全程 uploading 状态锁」（进度 100% 解析期间禁用上传/取消/X/输入，防重复提交）。走查 smoke_relation_type.cjs 5/5 PASS、smoke_upload_lock.cjs 9/9 PASS。教训重现：**同文件多处 Edit 必须串行**（本次并行编辑两次互相覆盖致修复不完整） |
| ~~模板导入结果面板空白 + 导入的 pending 指标在管理页不可见~~（**已修** 2026-09-11，用户验收发现） | ① 后端 import_packs 返回 `{results:[], totals:{}}`，前端误读顶层 `created/skipped/status_counts` → 结果页恒显示空。修复：改读 totals + 逐包 results + notes，并加「待绑定数据指标暂不在目录出现」提示。② `GET /metrics` 默认 `status=active`，模板导入未绑数据集的指标为 pending → 管理页默认查不到（目录页隐藏 pending 是设计行为）。修复：指标管理工具栏加状态下拉（全部状态=默认/启用中/待绑定数据/已停用）。走查 smoke_import_result.cjs 8/8 PASS |

## 四、待用户决策 / 需要用户的事项

- **B7 验收**：按《AI原生BI平台_B7验收报告.md》第七节走查清单操作（一键 smoke + 浏览器端到端约 5 分钟）；B6 视觉走查项继续有效。
- 已知偏差（有意为之）：主键 ID 后端为 int，前端直接透传未转 string（规范"与后端不一致时须先确认"——沿用后端契约）。
- **阶段 2（B8 质检/增量导入 + B9 问数）已完成**（2026-09-14）。**B9 LLM 已配置 + 模型管理上线**（2026-09-14）：① 真实凭据放 `backend/env/.env.dev.local`（本地覆盖层，gitignore，不进 git）；② 新增「模型管理」页（admin 专享，`/llm-models`）+ `llm_models` 表 + `/api/llm/models` CRUD/启用切换/连通性测试接口——用户可自行登记多个 OpenAI 兼容模型、一键切换，**DB 启用记录 > env 兜底**，删除启用记录自动回退；api_key 全程脱敏返回。测试：pytest 224 passed（+10）；本地 Mock LLM 端到端 9/9 PASS（`backend/scripts/verify_llm_admin.py` + `mock_llm_server.py`，因公司网络屏蔽外网 LLM 用 Mock 验证全链路）。
- **执行方案 V1.3（第十一节）已定稿待用户确认**（2026-09-15）：对标 BI佐罗文章将剩余阶段全部细化为 MVP 颗粒度批次——B9.2 问数 2.0（维度拆解/筛选/排序/TopN）→ B10 异动检测（三道判断+周期基准+单层归因）→ B12 报告中心（**算写分离**：平台先算结论，LLM 只组织文字、数字占位符回填）→ B11 触达 → B13 自动建模 → B14 多层归因 → B15 企业化；7 条设计红线贯穿（数值单点出口/LLM 永不产数字/无 LLM 可用降级/行业无关/权限同源/缓存纪律/每批收尾）。下一步：等用户确认后从 B9.2-1 开工。

## 五、运行与验证（三种方式，任选）

解释器统一使用 managed venv：
`C:\Users\William Y Liang\.workbuddy\binaries\python\envs\default\Scripts\python.exe`

### 方式 1：终端脚本（推荐，最省事）

```bash
cd bi-platform
./scripts/bi.sh test                    # 跑测试 → 160 passed
./scripts/bi.sh run dev 8100            # 起服务（热重载）→ http://127.0.0.1:8100/docs
./scripts/bi.sh seed 100000             # 重新生成两套合成数据
```

Windows PowerShell / PyCharm 终端用同功能脚本：

```powershell
.\scripts\bi.ps1 test
.\scripts\bi.ps1 run -Env dev -Port 8100
.\scripts\bi.ps1 seed -Rows 100000
```

> `bi.ps1` 已保存为 **UTF-8 with BOM** 并在开头设置控制台 UTF-8 编码，
> 用于修正 Windows PowerShell 5.1 的中文乱码（曾出现"鍚姩鍚庣"）。
> 若仍乱码，在 PowerShell 里先执行一次 `chcp 65001` 再运行脚本。

### 方式 2：PyCharm（适合边改代码边调）

1. **解释器**：Settings → Python Interpreter → Add → Existing → 选上面的 venv `python.exe`
2. **跑测试**：Run/Debug Configurations → `+` → Python tests → pytest → Target: `bi-platform/backend/tests`；Working directory: `bi-platform/backend`；Environment variables: `APP_ENV=test`
3. **起服务**：Run/Debug Configurations → `+` → Python →
   - Module name: `uvicorn`
   - Parameters: `app.main:app --reload --host 127.0.0.1 --port 8100`
   - Working directory: `bi-platform/backend`（**必须**，或用 PYTHONPATH 指向它）
   - Environment variables: `APP_ENV=dev`
   - 勾上 Run with Python Console 与否均可
4. **标记源码根**：右键 `bi-platform/backend` → Mark Directory as → Sources Root（消除 import 红线）

### 方式 3：让我在 WorkBuddy 里跑（适合回归验证）

说一句即可：「跑一遍测试」「起服务冒烟」「重新生成数据」。我会实际执行并贴出输出。
局限：我不能替你点击浏览器页面；B6 前端成型后，视觉走查仍需你本机打开。

### 本批交付的验看清单

1. 服务启动后浏览器打开 `http://127.0.0.1:8100/api/health` → 应见 `code:0`、`database:"up"`
2. 打开 `http://127.0.0.1:8100/docs` → FastAPI 自带 Swagger，可直接点接口调试
3. `bi-platform/data/synthetic/manifest.json` → 两套数据集的行数、编码、混杂列、异常值、表关系
4. `dataset_a/orders.csv` 用记事本打开显示乱码 = **正常**（GBK 编码靶点）
5. `bi-platform/data/metadata.db` 应约 12KB 且含 `key_value` 表（若为 0 字节说明建表失败，见第三节已修缺陷）

## 六、环境备忘

- Python：`C:\Users\William Y Liang\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（**3.13.14** managed venv，源码 3.11+ 兼容；文档中旧写的 3.13.12 已更正）
- 脚本：`scripts/bi.sh`（Git Bash，已实测）/ `scripts/bi.ps1`（PowerShell，**梁老师 2026-09-10 实测 test 分支通过 17 passed**）
- 已装依赖：fastapi、uvicorn[standard]、sqlalchemy≥2、pydantic≥2、pydantic-settings、duckdb≥1.0（B2）、**PyJWT≥2.8（B4）**、**pyyaml≥6.0（B5）**、pyarrow、openpyxl、charset-normalizer、pytest、httpx
- 项目代码根：`bi-platform/`（backend/ + scripts/ + data/，data 已 gitignore）
