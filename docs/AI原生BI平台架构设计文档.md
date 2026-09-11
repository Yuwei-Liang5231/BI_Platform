# AI 原生 BI 平台 架构设计文档

| 项 | 内容 |
| --- | --- |
| 文档版本 | V1.3 |
| 编制日期 | 2026-09-10 |
| 上游文档 | 《AI原生BI平台项目开发计划》V1.4 |
| 设计范围 | 全局架构概要（6 个阶段）+ 阶段 0+1 详细设计 |
| 架构形态 | 模块化单体（FastAPI 单应用） |
| 评审依据 | 《AI原生BI平台_可行性评估与文档匹配度审查》（2026-09-10） |
| 品牌与 UI 依据 | 《前端开发规范V1.0.0》（`~/.workbuddy/skills/pwc-ui-skill/`）+ `pwc-ui-skill`（PwC Core Library, Feb 2026）+ `pwc-brand`（品牌法与治理） |
| UI 体系判定 | **Product UI regime**（企业级数据平台 / dashboard / 内部工具），详见 D18 |

---

## 一、设计目标与核心不变式

### 1.1 设计目标

构建 AI 原生 BI 平台：口径集中定义，看板、问数、报告共用同一套指标；AI 负责理解与表达，数值统一由指标中心计算。对齐开发计划 V1.4 的阶段划分（主线 32 周 + 阶段 4b 可延后单元）。

### 1.2 核心不变式（贯穿所有阶段的架构约束）

1. **唯一取数出口**：所有数值消费端（看板、问数、报告、预警）只能通过 `query` 模块（统一计算服务）取数，这是"LLM 永不产生数字"和"口径只定义一次"的物理保障。
2. **权限双出口执行**：指标可见性过滤在 `query` 数值取数出口**强制执行**，同时指标目录/详情/搜索等元数据接口执行同一套过滤规则（受限指标的名称与口径不得对无权限用户泄露）——看板、目录、问数三端行为一致。
3. **口径变更原子生效**：指标定义变更 → 重新编译 → 存档更新 → 版本号递增 → 缓存自然失效，全链路在一个事务性流程内完成。
4. **数据存本地文件**：业务数据为用户上传的 Excel/CSV/TXT，质检后统一落地为 Parquet，由 DuckDB 直查；元数据存 SQLite 单文件。均不引入数据库服务器，但通过仓储/存储适配接口保留切换余地。
5. **预警必达（V1.1 新增）**：经三道过滤的异动必须通过 `alert` 的触达适配器送达用户常用通道（企业微信 / 邮件），站内经营总览仅作兜底与详情入口；触达失败须有重试与告警记录，不允许"预警存在于系统内但用户不知道"。
6. **表关系显式注册（V1.1 新增）**：跨表指标编译所依赖的表关系只能来源于 `dataset_relations` 的显式注册（人工确认或自动建模推荐后确认），禁止编译器隐式推断关联键。
7. **行业无关（V1.3 新增）**：平台**不得内置任何行业语义**。行业差异只能通过两层表达——① 数据层：`datasets` / `dataset_relations` / `dataset_coverage`；② 配置层：指标定义 `calc_rule`、行业指标模板、`topic` 枚举（配置文件而非硬编码）。**编译器、统一计算服务、元数据模型中禁止出现行业分支、行业专属字段或硬编码业务规则**。验证方式：同一套代码须在至少两个结构差异显著的行业数据集上跑通"定义 → 编译 → 计算"全链路，且无需修改任何产品代码。

---

## 二、关键架构决策记录

| # | 决策 | 结论 | 理由 |
| --- | --- | --- | --- |
| D1 | 文档范围 | 全局概要 + 阶段 0+1 详细设计 | 避免后期阶段过早固化；阶段 2+ 开工前再出各自详细设计 |
| D2 | 前端技术栈 | 严格执行《前端开发规范V1.0.0》（来源 `~/.workbuddy/skills/pwc-ui-skill/前端开发规范V1.0.0.md`）：Vue 3.5.34 + JavaScript（不启用 TS）+ Vite 8.0.13 + Pinia 3.0.4 + Vue Router 5.0.7 + Element Plus 2.14.0 + Axios 1.16.1 + Sass 1.99 + ECharts 6.1.0；核心依赖写**精确补丁版本**（禁 `^` 跨中版本漂移）、npm 锁文件 + `npm ci`、四环境 `dev/test/uat/pro` | 用户指定规范；与既有技术栈一致，本次补全版本矩阵与工程约束 |
| D3 | 元数据存储 | SQLite + SQLAlchemy 仓储抽象 | 文件级零部署；接口层留切 PostgreSQL 口子 |
| D4 | 业务数据存储 | 本地 Parquet 文件 + DuckDB 直查 | 用户指定"数据存本地"；千万行级无压力 |
| D5 | LLM 接入 | OpenAI 兼容接口（base_url/api_key/model 配置化） | 不绑定厂商，DeepSeek/豆包/通义/Ollama 均可切换 |
| D6 | 架构形态 | 模块化单体 | 6 人团队 + 32 周周期最匹配；模块边界清晰，未来可拆 |
| D7 | 指标模板库 | 内置电商零售 / SaaS互联网 / 连锁餐饮 / 通用经营 四套行业模板 | 解决阶段 0 口径梳理冷启动问题 |
| D8 | 部署约束 | 阶段 1 单进程部署（uvicorn 单 worker） | 规避 DuckDB 写并发限制与进程内缓存一致性问题 |
| D9 | 元数据库偏差 | SQLite 单文件替代计划原"数据库服务器（元数据）" | 用户指定数据存本地不介入数据库服务器；经仓储抽象保留切换余地，该偏差已与计划 V1.4 5.3 对齐 |
| D10 | 本地最小账号体系 | 阶段 1 自建 JWT 登录 + 用户/角色管理 | 权限模型（角色可见性、口径修改权限）需要登录态与角色载体才能落地；区别于阶段 4b 的 SSO/LDAP 对接，属最小本地账号。计划 V1.4 3.2 已同步该表述 |
| D11 | HTTP 状态码与 body code 对齐 | HTTP 状态码与 ApiResponse.body.code 数值一致（401/403/400/500），业务成功统一 HTTP 200 + code 200 | 与前端规范"HTTP 状态码与 body code 同时判断"的拦截器契约对齐 |
| **D12** | **数据接入时序（V1.1 新增）** | **`datasource` 模块阶段 1 即启用最小子集**：管理员专用上传 → 编码识别 → 类型推断 → 落 Parquet → 数据集注册 → 计算覆盖区间；阶段 2 扩展为完整质检（行列级异常定位）与增量导入 | 解决阶段 1 的 M1/M2"三处数值一致"验收无真实数据的矛盾；导入写操作安排在低峰执行，规避 DuckDB 写并发（与 D8 不冲突） |
| **D13** | **数据集与表关系元数据（V1.1 新增）** | **新增 `datasets` / `dataset_relations` / `dataset_coverage` 三张表**；阶段 1 关联能力限定为"单事实表 + 直接关联维度表"的星型结构，多跳与多事实表留阶段 4a | 原 `calc_rule.operands` 允许跨表但无 Join 依据，编译器无从得知关联键与筛选方向；这也是阶段 4a"模型跟着字段关系生成"的最小落点与输入 |
| **D14** | **预警触达通道（V1.1 新增）** | **企业微信机器人 webhook 为主通道（按部门/角色配置）、邮件 SMTP 为备选、站内经营总览为兜底**；通过触达适配器封装，新增通道只加适配器；送达失败重试 3 次并入告警 | "主动预警"若无站外触达，用户不登录即等于无预警，主动服务价值落空 |
| **D15** | **指标层级与构成关系（V1.1 新增）** | **`metrics` 增加 `level` 与 `parent_id`**；目录按层级展开，归因分解复用构成关系（父指标变化 ≈ 子指标贡献之和） | 对齐"按业务主题和层级整理"的目录形态，并为阶段 3 归因、阶段 4a 多层下钻提供结构化输入 |
| **D16** | **自动建模预研前移（V1.1 新增）** | 阶段 1 末安排 1–2 周 spike，输出《自动建模技术预研报告》（见 7.11），结论决定阶段 4a 范围；预置降级方案"自动推荐表关系 + 人工确认 + 可解释性提示" | 全项目唯一技术无人区，原安排在第 27–32 周过晚；提前 20 周暴露风险，避免已投入 100+ 人周后才发现不可行 |
| **D17** | **理解卡默认算法可配置（V1.1 新增）** | `metrics.disambiguation` 以 JSON 登记常见歧义场景的默认算法（如"下降"按减少金额还是降幅），由指标责任人维护；理解卡展示时明确引用该配置 | 原设计中默认值只能硬编码，无法满足"系统须交代采用哪种默认算法"且不同指标诉求不同 |
| **D18** | **UI 体系与品牌落地（V1.2 新增）** | 判定为 **Product UI regime**（企业级数据平台）；① 直接复用 `pwc-ui-skill/presets/element-plus/src/styles/`（tokens + element-overrides + index），`main.js` 中 `import '@/styles/index.scss'`，不手写 token 文件；② 5 个品牌 TTF 从 `~/.workbuddy/skills/pwc-brand/fonts/` 复制到 `public/fonts/`，按标准 `@font-face` 声明并带 CJK 回退；③ Logo 用内联 SVG，**不得置于橙色底**，最小高 48px；④ 默认圆角 0px（焦点环 4px、chip/头像 100%）；⑤ **默认仅浅色模式**，不生成暗色主题或主题切换；⑥ 每个生成页面标注 `<!-- pwc-regime: product-ui -->` | 项目栈（Vue 3 + Element Plus）与 preset 完全匹配；品牌 token 以"叠加层"方式落地，不重写组件库、不迁移技术栈 |
| **D19** | **指标涨跌配色语义（V1.2 新增，V1.3 已确认）** | **采用 PwC 状态色语义**：向好 = Green `#059669`，恶化 = Red `#DC2626`，**必须同时配箭头与文字**（不依赖颜色单通道传达信息）；不使用"红涨绿跌" | 已由用户确认（2026-09-10）；本平台为经营分析而非证券行情，与 PwC dataviz 状态色语义一致，且避免"红=亏损"的财务直觉冲突；红/绿在白底上须校验 WCAG AA，并保留箭头/文字作为无障碍冗余 |
| **D20** | **前端依赖与 Node 版本（V1.2 新增）** | Node `^20.19.0` 或 `>=22.12.0`（本机用 managed 22.22.2）；包管理器固定 **npm**，提交 `package-lock.json`；`package.json` 声明 `"type": "module"` 与 `engines.node`；`build` 必须等价于 `build:pro`；禁止无 `--mode` 的 `vite build`、禁止 `.env.production`（统一 `pro`） | 与 Vite 8 / unplugin 21 / 32 的 engines 要求一致；避免 CI 误用默认 `production` mode 打错环境包 |
| **D21** | **合成数据与行业无关验证（V1.3 新增）** | 开发期生成 **≥2 套行业结构差异显著**的合成数据集作为验证载体：① **零售交易型**（orders / users / products / channels，订单粒度）；② **订阅服务型**（subscriptions / accounts / plans / usage_events，含订阅周期与经常性收入语义）。生成脚本置于 `scripts/`（**非业务代码，不进 `backend/app`**），支持按 profile 扩展第三套（如连锁门店型）。**验收：同一套编译器在两套数据集上均完成"定义 → 编译 → 计算 → 导出"，且不修改任何产品代码** | 用户明确要求"支持各种业务数据，而非仅电商"；两套差异数据集能在阶段 1 早期暴露 v1 算子覆盖不足（如订阅周期、cohort 类语义），避免后期才发现行业适配缺陷 |
| **D22** | **后端运行时（V1.3 新增）** | **Python 3.13.12**（managed 独立 venv，与环境隔离）；源码保持 **3.11+ 兼容**，不使用 3.12/3.13 专属语法与标准库新增项 | 用户无版本要求，取最稳定组合；managed 运行时为推荐默认；保持 3.11 兼容可确保部署到 3.11 环境无需改动 |

---

## 三、全局架构图

```
┌─────────────────────────────────────────────────────┐
│  前端 Vue 3 (Vite 8 + Pinia + Element Plus + ECharts) │
│  登录 │ 指标目录 │ 指标详情 │ 统一看板 │ 指标管理        │
│  (阶段1) 数据集/表关系管理                              │
│  (阶段2) 数据源管理/理解卡/问数 (阶段3) 预警/报告        │
└──────────────────── HTTP/JSON (ApiResponse) ─────────┘
┌─────────────────────────────────────────────────────┐
│              FastAPI 模块化单体（后端）                │
│  auth(权限)  metric(指标中心)  query(统一计算引擎)     │
│  datasource(阶段1最小接入/阶段2完整质检+表关系)        │
│  agent(问数)  alert(预警+触达)  report(报告)          │
│  templates(指标模板库)                                │
└──────────┬──────────────────┬───────────────────────┘
           │                  │
   ┌───────▼──────────┐   ┌───▼─────────────────────────┐
   │ SQLite 元数据库    │   │ DuckDB(进程内) → Parquet     │
   │ 指标定义/用户/角色/ │   │ (上传的 Excel/CSV/TXT        │
   │ 变更记录/          │   │  质检后统一落地为列存文件)    │
   │ datasets+relations │   │  Parquet 由 pyarrow 写入     │
   │ +coverage(V1.1)   │   └─────────────────────────────┘
   └──────────────────┘
           │
   ┌───────▼─────────────────────────┐   ┌──────────────────────┐
   │ LLM 网关（OpenAI 兼容，配置化）    │   │ 触达适配器（V1.1 新增） │
   │ DeepSeek / 豆包 / 通义 / Ollama…  │   │ 企微机器人 / SMTP 邮件 │
   └─────────────────────────────────┘   └──────────────────────┘
```

### 3.1 阶段与模块对应

| 计划阶段 | 后端模块 | 前端页面 |
| --- | --- | --- |
| 阶段 0+1（第 1–12 周） | `auth` + `metric` + `query` + `templates` + **`datasource`（最小接入 + 数据集/表关系）** | 登录、指标目录、指标详情、统一看板、指标管理、**数据集与表关系管理** |
| 阶段 2（第 13–18 周） | `datasource`（完整质检 + 增量导入）+ `agent` + `infra/llm` | 数据源管理、理解卡、问数对话 |
| 阶段 3（第 19–26 周） | `alert`（+ 触达适配器）+ `report` + APScheduler | 预警中心、经营总览、报告查看 |
| 阶段 4a（第 27–32 周） | 自动建模引擎（复用 `query` 的指标定义与 `dataset_relations`） | 建模结果确认、多层归因分析 |
| 阶段 4b（第 29–36 周） | 行级权限、SSO/LDAP 对接、审批流与审计 | 行级权限配置、审计查询 |

---

## 四、技术栈

| 层 | 选型 | 版本/说明 |
| --- | --- | --- |
| 前端 | Vue 3 + Composition API（`<script setup>`） | 3.5.34，JavaScript（不启用 TS） |
| 前端构建 | Vite | 8.0.13，四环境 dev/test/uat/pro |
| 状态管理 | Pinia | 3.0.4（仅 setup store，禁 Options 写法） |
| 路由 | Vue Router | 5.0.7（与 Vue 3.5+ 配套） |
| 组件库 | Element Plus | 2.14.0（含 `@element-plus/icons-vue` 2.3.2） |
| HTTP | Axios | 1.16.1（统一封装 `src/config/request.js`，禁裸 axios） |
| 样式 | Sass + PwC Element Plus preset | 1.99.x；`src/styles/` 复用 `pwc-ui-skill/presets/element-plus` |
| 工程化 | ESLint 10.4（Flat Config）+ Prettier 3.8.3；unplugin-auto-import 21 / unplugin-vue-components 32 | 自动引入，JS 项目不生成 dts |
| 图表 | ECharts | 6.1.0，配色遵循 PwC dataviz + D19 状态色 |
| 前端运行时 | Node ≥ 22.12.0（managed 22.22.2）；包管理器 npm | 与 Vite 8 engines 一致 |
| 品牌字体 | ITC Charter（Display/标题）+ Helvetica Neue（正文/UI/数据） | 5 个 TTF，含 CJK 回退（PingFang SC / Microsoft YaHei / Songti SC） |
| 后端 | Python / FastAPI | **Python 3.13.12**（managed 独立 venv，D22）；源码保持 3.11+ 兼容；uvicorn 单进程部署 |
| 计算引擎 | DuckDB | 进程内嵌入式，直查 Parquet |
| 列存写入 | pyarrow | Parquet 落地（不经 DuckDB 写连接） |
| 元数据 | SQLite + SQLAlchemy 2.x | 仓储接口抽象，留 PostgreSQL 切换口 |
| Agent | Deep Agents | 阶段 2 问数智能体 |
| LLM | OpenAI 兼容 SDK | base_url/api_key/model 全配置化 |
| 调度 | APScheduler | 阶段 3 |
| **触达通道（V1.1）** | **企业微信机器人 webhook / SMTP** | 阶段 3；适配器封装，凭据走 `.env` |
| 报告 | python-docx | 阶段 3 Word 输出 |

---

## 五、后端模块划分与目录结构

```
backend/
├── app/
│   ├── main.py                    # FastAPI 入口
│   ├── core/
│   │   ├── config.py              # .env 驱动（LLM、数据路径、触达凭据、环境）
│   │   └── response.py            # ApiResponse {code, message, data} 统一结构
│   ├── infra/                     # 技术设施层（切换口子所在）
│   │   ├── duckdb_session.py      # DuckDB 连接管理：每请求内存连接（读 Parquet）
│   │   ├── repository/            # 元数据仓储接口 + SQLite 实现（可切 PG）
│   │   ├── storage/               # 数据存储适配接口 + Parquet 实现（可扩展）
│   │   ├── cache.py               # 指标结果缓存（键含指标版本号 + 数据集版本号）
│   │   ├── notify/                # (阶段3) 触达适配器：企微 / SMTP / 站内
│   │   └── llm/                   # OpenAI 兼容客户端（阶段 2 启用）
│   └── modules/
│       ├── auth/                  # 用户、角色、登录、权限判断能力提供
│       ├── metric/                # 指标 CRUD、口径变更记录、目录/搜索、层级
│       ├── query/                 # 指标编译器 + 统一计算服务（唯一取数出口）
│       ├── templates/             # 行业指标模板库（种子数据 + 批量导入）
│       ├── datasource/            # (阶段1) 最小接入 + 数据集/表关系注册
│       │                          # (阶段2) 完整质检 + 增量导入 + 覆盖区间刷新
│       ├── agent/                 # (阶段2) 问数：理解卡 → 意图 → query 取数
│       ├── alert/                 # (阶段3) 异动检测、三道过滤、限流、触达
│       └── report/                # (阶段3) 归因分解 + LLM 组织成文 + Word
├── data/                          # 运行时数据目录（gitignore）
│   ├── uploads/                  # 原始上传文件
│   ├── parquet/                   # 质检后列存数据
│   └── metadata.db                # SQLite 元数据库
└── tests/
```

### 5.1 模块职责边界

| 模块 | 职责 | 依赖 |
| --- | --- | --- |
| `auth` | 用户/角色管理、登录态、提供"用户可见哪些指标 / 可改哪些指标"的判断能力（不直接过滤数据） | infra/repository |
| `metric` | 指标定义全生命周期：CRUD、版本与变更记录、目录搜索（名称/别名）、**层级与构成关系**、**歧义默认算法配置**、`disambiguation` | auth（写权限校验）、query（变更时触发重编译） |
| `query` | 指标编译器（定义 + **表关系** → 参数化 SQL）、统一计算、结果缓存、**在取数出口执行可见性过滤** | infra 全部、auth（权限判断）、datasource（表关系与覆盖区间） |
| `templates` | 行业模板种子数据（YAML）、模板列表、批量导入为正式指标 | metric |
| `datasource` | **（阶段 1）**文件上传、编码识别、类型推断、Parquet 落地、`datasets`/`dataset_relations` 注册、`dataset_coverage` 计算；**（阶段 2）**行列级质检、增量导入、覆盖区间刷新与缓存失效 | infra/storage、infra/repository |
| `agent` | 问数：LLM 意图解析、理解卡生成、歧义提示（引用 `disambiguation`）；数值一律调 query | query、infra/llm |
| `alert` | 定时巡检、周期性修正、三道过滤、限流、**经 infra/notify 触达送达** | query、infra/notify |
| `report` | 结构性归因分解（算）、LLM 组织成文（写）、Word 输出、数字溯源 | query、infra/llm |

---

## 六、关键数据流

### 6.1 阶段 0+1：接入 → 指标中心 → 看板

```
【数据接入（V1.1 新增，阶段 1 最小版）】
管理员上传 Excel/CSV/TXT
  → 编码识别（GBK/UTF-8）→ 类型推断 → 结构解析
  → 落 Parquet（pyarrow）→ 注册 datasets（表/字段/时间字段）
  → 人工确认表关系 → 写 dataset_relations
  → 计算 dataset_coverage（实际覆盖区间）→ 数据集版本号 dataset_ver 递增

【指标定义与编译】
管理员录入指标定义（或从行业模板导入）
  → metric 模块（校验口径修改权限：指标责任人 ∪ 数据团队）
  → SQLite 落库 + metric_changes 留痕
  → query 编译器（引用 dataset_relations 生成 Join）生成参数化 SQL 并存档
  → 指标版本号 ver 递增 → 缓存键变化，旧缓存自然失效

【看板取数】
  → query 模块
  → 权限过滤（auth 能力）
  → 周期完整性判定（依据 dataset_coverage；不完整返回 null）
  → 查缓存（键 = 指标ID + ver + dataset_ver + 维度组合 + 时间范围）
  → 未命中则执行编译 SQL → DuckDB 只读连接查 Parquet → 写缓存
  → 返回数值
```

### 6.2 阶段 2：问数（预设演进，详细设计开工前补充）

```
用户提问 → LLM 意图解析（指标/时间/维度/排序）
  → 歧义检测：有歧义先出理解卡（黄色提示引用 metrics.disambiguation 的默认算法），用户可改
  → 权限预检：不可见指标在理解卡阶段即提示"该指标无权限"
  → 用户确认 → query 取数（与看板同一出口、同一权限过滤）
  → 返回结果；找不到可用指标明确返回"算不了"

增量导入 → 刷新 dataset_coverage → dataset_ver 递增 → 相关指标缓存自然失效
```

### 6.3 阶段 3：预警与报告（预设演进）

```
APScheduler 定时巡检 → query 取历史与当前值
  → 异动检测（周期性修正：周日对比历史多个周日）
  → 三道过滤（反常性/业务要紧性/可归因性）→ 限流（日报 ≤ 3 条）
  → 【V1.1 新增】触达适配器推送：企微机器人（主）→ 失败重试 3 次 → SMTP 邮件（备）
  → 送达结果落库（notify_log），连续失败触发系统告警
  → 站内经营总览作为兜底与详情入口
报告：归因分解（维度贡献度，query 取数）→ LLM 组织成文 → Word 输出
  → 每个结论关联指标ID与取数参数，可回溯
```

---

## 七、阶段 0+1 详细设计

### 7.1 元数据模型（SQLite）

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `users` | id(TEXT/UUID), username, password_hash, display_name, department, status | 主键统一 string；department 记录责任部门 |
| `roles` | id, code, name | 预置：admin / data_team / viewer 及业务角色 |
| `user_roles` | user_id, role_id | 多对多 |
| **`datasets`（V1.1 新增）** | id, code, name, source_type(upload), table_name, file_path, fields(JSON), time_field, dataset_ver, status | 数据集/表注册；`fields` 记录字段名、推断类型、样例值；`dataset_ver` 数据变更时递增（纳入缓存键） |
| **`dataset_relations`（V1.1 新增）** | id, left_dataset_id, left_field, right_dataset_id, right_field, join_type(inner/left), is_default, source(manual/recommended), description | 表关系显式注册；`source=recommended` 为阶段 4a 自动建模推荐、待人工确认的记录；阶段 1 仅支持星型（单事实表 + 直接关联维度表） |
| **`dataset_coverage`（V1.1 新增）** | id, dataset_id, min_date, max_date, grain, row_count, refreshed_at | 数据实际覆盖区间，为"周期完整性留空"判定提供依据（V1.0 中该信息无来源） |
| `metrics` | id, code, name, aliases(TEXT,JSON数组), definition, calc_rule(JSON,见 7.2), dimensions(JSON), filters(JSON), topic, **level, parent_id(V1.1)**, **disambiguation(JSON, V1.1)**, **primary_dataset_id(V1.1)**, owner_user_id, owner_department, status, ver | `ver` 指标版本号；`level`/`parent_id` 支撑目录层级与归因构成关系；`disambiguation` 登记歧义场景默认算法（D17） |
| `metric_changes` | id, metric_id, before(JSON), after(JSON), reason, operator_id, created_at | 口径审计留痕 |
| `metric_visibility_restriction` | metric_id, role_id | **受限登记表**：只登记"受限指标 → 可访问角色"；未登记的指标对所有登录用户默认可见 |
| `metric_sql_archive` | metric_id, ver, sql_text, compiled_at | 编译存档，详情页折叠区展示；亦用于"按历史口径回看" |
| `dashboards` | id, name, topic, sort_order, status | 看板（按业务主题一版一块） |
| `dashboard_cards` | id, dashboard_id, metric_id, title, chart_type(line/bar/pie/number), dimensions(JSON), default_time_range, sort_order | 看板卡片，引用指标；可见性随指标推导 |
| **`notify_log`（阶段 3，V1.1 预留）** | id, alert_id, channel, target, status, retry_count, sent_at, error | 触达送达记录，支撑"送达率 ≥ 99%"验收 |

> 所有表含 `created_at` / `updated_at`。字典/枚举（业务主题等）用配置文件而非表。

### 7.2 指标编译器

**calc_rule 结构化规范（JSON Schema）**——指标计算规则不写自然语言，用受限结构表达：

```json
{
  "base_aggregation": "sum | count | count_distinct | avg | max | min",
  "source": { "table": "orders", "column": "amount", "filter": "status = 'paid'" },
  "expression": "A / B",              // 可选：由若干命名子聚合组成四则运算
  "operands": {                        // expression 引用的子聚合，每个为扁平结构 {table, column, aggregation, filter}
    "A": { "table": "orders", "column": "amount", "aggregation": "sum", "filter": "status = 'paid'" },
    "B": { "table": "orders", "column": "user_id", "aggregation": "count_distinct" }
  },
  "time_field": "created_at",         // 时间对齐字段（跨表时各 operand 指定各自的）
  "time_grain_default": "day | week | month"
}
```

**跨表 Join 表达（V1.1 补充）**：当 `operands` 中的 `table` 分属不同 `datasets` 时，编译器**只能**从 `dataset_relations` 中解析关联路径：
- 阶段 1：仅支持"单事实表 + 直接关联维度表"的星型结构（`is_default=true` 的关系优先）；
- 找不到显式注册关系时**保存即拒绝**并明确报错"缺少 orders → users 的表关系，请先在数据集管理中登记"，禁止隐式推断（不变式 6）；
- 多跳路径与多事实表：阶段 4a 随自动建模引擎扩展。

**编译器 v1 算子清单（超出即保存时拒绝，给出明确报错）**：
- 单层聚合（SUM/COUNT/COUNT DISTINCT/AVG/MAX/MIN）+ 行级过滤（字段/枚举/数值比较的合取范式）
- 子聚合间的四则运算（支撑比率类：客单价 = 金额/用户数、退货率 = 退货单/总单、毛利率、满减核销率等）
- 跨表 Join（受 `dataset_relations` 约束，星型，见上）
- 时间维度下钻与时间窗对齐（所有 operand 强制对齐同一 `time_field` 与时间范围，避免分子分母口径漂移）
- 环比/同比：由查询层在编译产物之上计算（本期值 vs 上一期值），不属于编译器表达式

**明确不支持（v1）**：cohort 分析（LTV 类需按注册月分组再聚合）、窗口函数、嵌套聚合、自定义 SQL 片段、多跳/多事实表关联。模板库中的 LTV 指标注明"需阶段 4a 扩展算子"，导入时默认标记为不可计算口径。不支持项在保存时校验拒绝并提示。

**周期完整性（"留空"机制）**：编译产物附带周期元信息（时间粒度、数据实际覆盖区间）；查询层据此判定请求的观察周期是否完整——不完整时返回 `null`（前端显示横线"—"），**绝不返回 0% 或截断值**。该规则对目录卡片、看板、导出统一生效。

- **覆盖区间来源（V1.1 明确）**：`dataset_coverage` 表，由 `datasource` 在数据接入/增量导入后计算并刷新（V1.0 中该信息缺少来源）；
- 输入：指标定义（calc_rule 结构化 JSON）+ `dataset_relations` + `dataset_coverage`
- 输出：参数化 SQL（时间范围、维度取值为绑定参数），DuckDB 方言
- **编译存档与定义原子同步**：口径变更流程内重新编译、更新 `metric_sql_archive`，存档 SQL 与实际执行 SQL 完全一致（满足"详情页查询语句与实际执行逻辑一致"验收标准）
- 编译器接口抽象（`compile(metric_def, relations, coverage) -> CompiledQuery`），为阶段 4a 自动建模预留复用

**目录卡片数值语义（当前值/环比/小趋势）**：当前值 = 该指标默认时间粒度下最近一个**完整周期**的值；环比 = 与上一完整周期比较（不完整周期不参与，显示横线）；小趋势 = 最近 12 个完整周期的序列。默认时间范围可被卡片配置覆盖（`dashboard_cards.default_time_range`）。

### 7.3 统一计算服务（query）

- API 形态：`POST /api/query/metric-value`（指标ID、时间范围、维度组合、筛选）
- 出口统一执行：权限过滤 → 周期完整性判定 → 缓存查询 → 编译 SQL 执行 → 返回
- **DuckDB 连接策略**：查询使用纯内存连接（`duckdb.connect()`，不附着 .duckdb 数据库文件），每次查询通过 `read_parquet('data/parquet/xx/*.parquet')` glob 方式解析文件清单——天然感知新增文件，无需连接失效处理；**Parquet 落地由 pyarrow 写入（不经 DuckDB 写连接）**，与查询只读策略完全隔离。连接管理：每请求创建内存连接，量级上来后再引入只读连接复用
- **写操作说明（V1.1 修正）**：V1.0 曾表述"写操作（数据导入）发生在阶段 2，阶段 1 数据为只读"——**该表述已被 D12 取代**。阶段 1 即提供管理员专用的最小导入能力，安排在业务低峰执行；单进程部署（D8）下不存在并发写冲突，导入完成后刷新 `dataset_coverage` 并递增 `dataset_ver`
- **缓存**：进程内 LRU（键 = 指标ID:ver:dataset_ver:维度:时间范围），指标版本或数据集版本变更后旧键永不命中，自然失效，无需主动清理；单进程部署保证一致性（决策 D8）

### 7.4 权限模型（阶段 1 最小版）

| 能力 | 规则 |
| --- | --- |
| 登录 | 全部功能需登录（JWT），无匿名访问 |
| 指标可见性 | **默认可见，显式受限**：`metric_visibility_restriction` 只登记"受限指标 → 可访问角色"。判定规则：指标未出现在登记表 → 所有登录用户可见；指标已登记 → 仅拥有登记角色的用户可见 |
| 目录/详情过滤 | `GET /api/metrics/page`、`GET /api/metrics/{id}`、模板导入预览等元数据接口执行同一套判定——无权限时目录不返回该指标（详情接口返回 403），受限指标的名称与口径不泄露 |
| 看板可见性 | 看板卡片引用指标，可见性取指标判定结果；受限指标对无权限用户隐藏卡片并显示"无权限"占位 |
| 口径修改权限 | 指标责任人（owner_user_id）∪ data_team 角色可改；其他人对指标定义为只读 |
| **数据集管理权限（V1.1）** | 数据接入与表关系维护仅 admin ∪ data_team 可操作；业务角色只读 |
| 可见性配置权限 | 仅 admin ∪ data_team 可维护 `metric_visibility_restriction` |
| 变更留痕 | 所有写操作记入 metric_changes（操作人、时间、前后值、原因） |
| 执行位置 | auth 提供判断能力；数值出口（query）与元数据接口（目录/详情）双出口执行同一套过滤，看板与（阶段 2）问数共用 |

### 7.5 行业指标模板库

- 位置：`backend/app/modules/templates/seed/`，YAML 格式，随代码版本管理，可随时追加行业
- 结构：每个行业一个文件（`ecommerce.yaml` / `saas.yaml` / `restaurant.yaml` / `general.yaml`），每条含：编码、名称、别名列表、业务定义、计算规则（calc_rule 结构，见 7.2）、维度建议、**层级建议（level/parent 编码）**、常见口径分歧说明（如复购率的三种算法）与**默认算法建议（预填 disambiguation）**
- 规模：每行业 20~30 条，共约 100 条；其中依赖 v1 不支持算子的指标（如 LTV 的 cohort 分析）标注 `requires: extended-operator`，导入后状态为"口径已登记、暂不可计算"
- API：`GET /api/templates/industries`（行业列表+指标数）、`GET /api/templates/{industry}`（明细）、`POST /api/templates/import`（勾选批量导入为正式指标）
- 导入后即为普通指标：分配责任人（导入人）、进入统一权限/编译/看板流程；已存在同编码指标则跳过并提示；导入时若缺 `primary_dataset_id`，状态标记为"待绑定数据集"
- 前端：指标管理页新增"从模板导入"向导（选行业 → 预览勾选 → 导入结果）

内置行业（决策 D7）：

| 行业 | 示例指标 |
| --- | --- |
| 电商零售 | GMV、客单价、复购率、退货率、毛利率、满减核销率、渠道占比、准时发货率 |
| SaaS/互联网 | MRR、ARR、客户流失率、NDR、DAU/MAU、CAC、LTV |
| 连锁餐饮 | 单店日均营业额、翻台率、坪效、外卖占比、会员复购率 |
| 通用经营 | 营收、毛利率、费用率、应收周转天数、库存周转天数 |

### 7.6 API 规范（与前端规范对齐）

- 统一响应：`{code, message, data}`；**HTTP 状态码与 body code 数值对齐**（决策 D11）：业务成功 HTTP 200 + code 200；未登录 HTTP 401 + code 401；无权限 HTTP 403 + code 403；参数/业务校验失败 HTTP 400 + code 400；服务端错误 HTTP 500 + code 500
- 分页：`{list, total, pageNum, pageSize}`；主键 ID 一律 string（UUID）
- 阶段 1 主要接口：

| 模块 | 接口 | 说明 |
| --- | --- | --- |
| auth | `POST /api/auth/login`、`GET /api/auth/me`、`GET/POST/PUT/DELETE /api/auth/users`、`GET/POST /api/auth/roles` | 用户/角色管理仅 admin；PUT 支持调整角色 |
| metric | `GET /api/metrics/page`（目录搜索，含可见性过滤与层级）、`GET /api/metrics/{id}`、`POST/PUT/DELETE /api/metrics`、`GET /api/metrics/{id}/changes`、`GET/PUT /api/metrics/{id}/visibility` | 写操作过口径修改权限；visibility 为受限登记维护 |
| **datasource（V1.1 前移）** | `POST /api/datasource/upload`（阶段1最小接入：编码识别+类型推断+落 Parquet+注册）、`GET /api/datasets`、`GET /api/datasets/{id}`、`POST /api/datasets/{id}/refresh-coverage`、`GET/POST/PUT/DELETE /api/datasets/{id}/relations` | 上传与表关系维护仅 admin ∪ data_team；阶段 2 扩展 `POST /api/datasource/validate`（完整质检）与增量导入 |
| query | `POST /api/query/metric-value`、`GET /api/query/dashboard` | 看板聚合取数 |
| export | `GET /api/export/metric-value`（指标ID、时间范围、维度，Excel 输出） | **挂载于 query 模块内**，复用 query 取数出口，保证"看板、明细、导出三处数值一致" |
| templates | 见 7.5 | 模板导入 |
| 前端 Controller 对应 | `src/api/{auth,metric,query,export,templates,datasource}/` | 严格遵守前端规范 |

### 7.7 前端页面（阶段 1）

| 页面 | 路由 | 内容 |
| --- | --- | --- |
| 登录页 | `/login` | 用户名密码 |
| 指标目录 | `/metrics` | 主题分组 + **层级展开**卡片、中文名/别名搜索（结果已过可见性过滤）、当前值/环比/小趋势（ECharts mini，不完整周期显示"—"） |
| 指标详情 | `/metrics/:id` | 口径说明、为什么这样定、易错点、变更历史、折叠区展示存档 SQL |
| 统一看板 | `/dashboard` | 主题分组图表（ECharts）、无权限指标占位、导出入口（Excel） |
| 指标管理 | `/metrics/admin` | CRUD（权限受限）、从模板导入向导、变更记录查看、受限可见性配置、**歧义默认算法配置** |
| **数据集与表关系管理（V1.1）** | `/datasets` | 数据集列表与字段预览、上传接入、**表关系可视化配置（左表·字段 → 右表·字段、关联类型、设为默认）**、覆盖区间显示与刷新 |

四环境（dev/test/uat/pro）、目录结构、Axios 封装（`src/config/request.js` 解包 `data`、401 跳登录、403 仅提示）、Pinia setup store、路由命名、`src/api/{module}/` 分层等**全部按《前端开发规范V1.0.0》执行**；品牌与视觉部分按 **7.12** 执行。

**页面级品牌要点**：登录页是本项目唯一的 primary surface，须含 PwC Logo 与品牌橙（不使用 Momentum Mark，内部工具无需）；其余页面为 secondary surface，遵循 personality 与全部 token 标准。所有页面文件顶部标注 `<!-- pwc-regime: product-ui -->`。

### 7.8 错误处理

- LLM（阶段 2 起）不可用：问数/报告功能返回明确错误，看板/目录等非 LLM 功能完全不受影响
- 质检失败：行列级定位信息返回，数据不落地（阶段 1 最小接入下，类型推断失败给出明确提示并允许人工修正后重试）
- **表关系缺失（V1.1）**：编译期报错需明确指出缺哪两张表之间的关系，并给出跳转至 `/datasets` 的引导
- 编译失败：指标定义保存时即校验可编译性，保存与编译存档同一事务，杜绝"定义存在但编译不出 SQL"的脏状态
- 无权限：403 / 看板占位 / 理解卡提示，三端行为一致
- **触达失败（阶段 3）**：重试 3 次后仍失败则落 `notify_log` 并触发系统告警，不影响站内预警可见

### 7.9 数据接入（阶段 1 最小版，V1.1 新增）

对应决策 D12，解决"阶段 1 无真实数据可验收"的问题。

| 环节 | 实现要点 |
| --- | --- |
| 上传 | 支持 Excel/CSV/TXT；仅 admin ∪ data_team 可操作；建议低峰执行 |
| 编码识别 | 按 UTF-8 → GBK → GB18030 顺序探测，识别结果在前端展示并允许人工纠正 |
| 类型推断 | 逐列推断（数值/日期/字符串）；混杂列（数值中夹文字）标记 `mixed` 并列出异常样例行号，由人工决定"置空/转文本/拒绝导入" |
| 落 Parquet | pyarrow 写入 `data/parquet/{dataset_code}/`；原始上传文件保留于 `data/uploads/` 作为可重放来源 |
| 数据集注册 | 写 `datasets`（table_name、fields、time_field）；`dataset_ver` 初始化为 1 |
| 表关系登记 | 前端可视化配置（`/datasets`），写 `dataset_relations`；阶段 1 支持星型，`source=manual` |
| 覆盖区间计算 | 写 `dataset_coverage`（min_date/max_date/grain/row_count），为"留空机制"提供依据 |
| 缓存失效 | `dataset_ver` 递增 → 相关指标缓存键变化 → 自然失效（覆盖"指标定义未变而数据变化"的场景） |

**阶段 2 在此之上扩展**：行列级质检报告、增量导入策略、覆盖区间自动刷新与调度。

### 7.10 预警触达通道（阶段 3 概要定义，V1.1 新增）

对应决策 D14 与不变式 5，"主动预警"必须有站外触达能力。

| 项 | 设计 |
| --- | --- |
| 通道优先级 | 企业微信机器人 webhook（主）→ 邮件 SMTP（备）→ 站内经营总览（兜底与详情入口） |
| 适配器 | `infra/notify/` 下 `WecomNotifier` / `SmtpNotifier` / `InboxNotifier` 实现统一 `Notifier` 接口；新增通道只加适配器 |
| 目标配置 | 按部门/角色配置 webhook 与收件人（存配置表或配置文件，阶段 3 详细设计定） |
| 限流与频率 | 日报 ≤ 3 条（与现有限流一致）；紧急异动可单独配置即时推送；明确推送时段（如工作日 09:00）与免打扰规则 |
| 送达保障 | 失败重试 3 次（指数退避）；结果落 `notify_log`；连续失败触发系统告警 |
| 验收挂钩 | 触达送达率 ≥ 99%；试运行期统计点击/处理率，作为"预警是否真的被用"的度量 |
| 凭据 | webhook 地址与 SMTP 账号走 `.env.{env}`，不入库、不进代码仓库 |

### 7.11 自动建模技术预研结论（V1.1 新增，待填写）

对应决策 D16 与计划里程碑 M1a（第 12 周末）。**本节在预研完成后填写**，需回答：

1. **可行性判定**：能否从指标定义 + `dataset_relations` + 字段统计特征推导出可用数据模型？
2. **最小可用方案**：若可行，给出方法（如基于字段基数/命名相似度/外键重合度推荐关联，结合指标口径验证）；若不可行，明确降级方案（自动推荐 + 人工确认 + 可解释性提示，目标推荐准确率 ≥ 80%）。
3. **对阶段 4a 的影响**：范围、工期、验收标准是否需要调整。
4. **对现有架构的影响**：`compile()` 接口与 `dataset_relations` 是否需要扩展。

> 预研结论未产出前，阶段 4a 不得进入开发排期。

### 7.12 品牌与 UI 落地规范（V1.2 新增，批次 B6 依据）

对应决策 D18 / D19 / D20。资源均已在本机核验存在。

| 项 | 落地方式 | 来源 |
| --- | --- | --- |
| **UI 体系** | Product UI regime（企业级数据平台 / dashboard） | `pwc-brand` → `core-library-bridge.md` |
| **样式基座** | 复制 `presets/element-plus/src/styles/`（tokens.scss / element-overrides.scss / index.scss）到 `src/styles/`，`main.js` 中 `import '@/styles/index.scss'` | `pwc-ui-skill/presets/element-plus/`（已核验存在） |
| **字体** | 5 个 TTF 复制到 `public/fonts/`，按标准 `@font-face` 声明（含 `local()` 与 `font-display: swap`），CSS 变量加 CJK 回退 | `~/.workbuddy/skills/pwc-brand/fonts/`（已核验存在） |
| **Logo** | 内联 SVG；容器底色仅白或黑，**不得放橙色底**；`min-height: 48px; width: auto;` | `logo-rules.md` |
| **色彩** | 品牌橙 `#FD5108` 仅作强调/CTA/图表强调；正文与链接一律黑或白；Orange 500 不作正文色（3.05:1 不达 AA） | `colour-tokens.md` |
| **圆角** | 全站 0px；焦点环 4px；chip/头像/radio 100% | `shape-tokens.md` |
| **栅格与间距** | 12 / 6 / 4 列（桌面 / 平板 / 移动），8px 基准间距 | `grid-tokens.md` / `spacing-tokens.md` |
| **组件状态** | 按钮/链接/输入框/选择项均需 enabled / hover / focus / pressed / disabled；主按钮 Tonal Filled（rest Orange 300 → hover Orange 500 → press 回 Orange 300） | `component-states.md` |
| **图表** | ECharts 配色取 PwC dataviz 序列（Orange 400/300/200/100、Grey 500/200/50）；涨跌语义按 **D19** | `dataviz.md` |
| **暗色模式** | **默认不生成**；如需暗色须显式提出，且一律用语义 token，不硬编码 | Critical Rule 7 |
| **禁止项** | 斜体、正文 letter-spacing、自造图标/插画/渐变、橙色大面积底填充、白字配黄底 | `pwc-brand` Critical Rules |
| **留空表达** | 观察周期不完整的指标显示"—"（横线），与品牌"敢说算不了"一致；禁用 0% 或灰色 0 | 项目不变式 + 品牌 personality |

**自检清单（每个前端批次交付前逐项确认）**：
- [ ] Logo 为内联 SVG，非文本近似，未置于橙色底
- [ ] 5 个品牌 TTF 已 `@font-face` 声明，标题用 ITC Charter、正文/数据用 Helvetica Neue，CJK 回退已配
- [ ] 颜色全部取自 token（无硬编码 hex 近似值）
- [ ] `border-radius` 全为 0（除焦点环 4px 与 chip/头像 100%）
- [ ] 按钮 / 链接 / 表单具 hover / focus / pressed / disabled 四态
- [ ] 无斜体、无正文字间距
- [ ] 未生成暗色主题或主题切换
- [ ] 栅格 12/6/4，触控目标 ≥ 44×44px
- [ ] 涨跌方向按 D19 且配箭头与文字（不单靠颜色）
- [ ] `npm run build:{dev,test,uat,pro}` 四环境全部通过

---

## 八、测试策略

| 层 | 重点 |
| --- | --- |
| 单元测试 | 指标编译器（定义→SQL 正确性，含边界：空维度/无筛选/四则运算/**跨表 Join 与关系缺失拒绝**/超范围算子被拒绝）、权限判定逻辑、周期完整性判定（不完整周期返回 null 而非 0）、模板导入幂等性 |
| 集成测试 | **口径一致性固定用例**：同一指标经看板接口、直接取数接口、导出接口三处数值一致；口径变更后缓存失效再取值为新口径；受限指标在目录/详情/取数/导出四端的行为一致；**数据接入后 dataset_ver 递增且缓存正确失效** |
| **跨行业一致性（V1.3 新增，对应不变式 7 / D21）** | 同一套编译器与取数服务，在**零售交易型**与**订阅服务型**两套合成数据集上独立完成"指标定义 → 编译 → 取数 → 导出"全链路；golden SQL 用例覆盖**两个行业各 ≥8 条**；断言无需修改任何产品代码即可支持新行业；行业专属语义（如订阅周期）若超出 v1 算子，须被明确拒绝并给出提示而非静默出错 |
| API 测试 | FastAPI TestClient 覆盖全部阶段 1 接口（新增 datasource 系列），含 401/403/400 场景及 HTTP 状态码与 body code 对齐校验 |
| **异动检测算法回归（V1.1 新增，阶段 3）** | 基于人工标注基线集的回归测试：每次阈值或算法调整后，在标注集上重跑，验证三道过滤联合准确率不劣化；周期性误报率作为固定断言 |
| **触达送达测试（V1.1 新增，阶段 3）** | 模拟 webhook/SMTP 失败与超时，验证重试、降级与 `notify_log` 记录正确 |
| 前端 | 按《前端开发规范V1.0.0》自检清单（四环境 build 通过、无裸 axios、无 Options API、401/403 仅在 `request.js` 处理、主键 ID 统一 string、分页字段名与后端一致）+ **7.12 品牌自检清单**（Logo / 字体 / token / 圆角 / 组件状态 / 无暗色模式） |

---

## 九、阶段 2+ 演进预留（概要）

- `infra/repository`：切换 PostgreSQL 只改实现与连接串
- `infra/storage`：新增数据源类型（数据库直连）只加适配器
- `infra/llm`：换模型只改配置
- `infra/notify`（V1.1）：新增触达通道只加适配器
- `query` 编译器接口：阶段 4a 自动建模引擎复用
- 拆分微服务：模块间仅经接口调用（无跨模块直接掏仓库），边界已保留

**阶段 2 详细设计必答项（V1.1 增补）**：

1. ~~Join 关系来源~~ → 已由 D13（`dataset_relations`）解决，阶段 2 需明确"推荐关系的产出与人工确认流程"；
2. **增量导入策略**：全量覆盖 vs 增量追加，与 `dataset_ver` 递增和 Parquet 文件组织的对应关系；
3. **覆盖区间刷新触发**：导入后自动刷新 vs 定时刷新，`dataset_coverage` 与缓存失效的时序；
4. **质检结果呈现**：行列级异常如何在前端可操作地修正（而非仅报错）。

**阶段 3 详细设计必答项（V1.1 新增）**：

1. 三道过滤的**基线算法与阈值配置方式**（反常性/要紧性/可归因性的具体公式与指标级覆盖）；
2. **人工标注基线集**的规模、标注规范与持续维护机制；
3. 触达目标的**配置模型**（部门/角色 → webhook/收件人）与免打扰规则。

---

## 十、部署与运维

### 10.1 部署拓扑

| 项 | 方案 |
| --- | --- |
| 后端 | uvicorn 单进程 + 进程守护（Windows：NSSM 服务；Linux：systemd），异常自动拉起 |
| 前端 | `npm run build:pro` 产物由 Nginx 托管静态资源，`/api` 反向代理到 FastAPI |
| 环境区分 | 后端与前端同样四环境（dev/test/uat/pro），后端 `.env.{env}` 驱动（数据库路径、LLM 配置、JWT 密钥、**企微 webhook、SMTP 账号**），与前端 `.env.{mode}` 一一对应 |
| CI/部署映射 | 前端按规范（测试环境 `build:test`、UAT `build:uat`、生产 `build:pro`），后端同环境独立部署 |
| **出网要求（V1.1）** | 阶段 3 起需允许访问企业微信机器人接口与 SMTP 服务器；如部署在内网隔离环境，须提前申请出网策略，否则触达通道不可用 |

### 10.2 备份与恢复

| 对象 | 策略 |
| --- | --- |
| SQLite 元数据库（指标口径资产 + 数据集与表关系） | 每日定时备份（`data/backups/metadata_YYYYMMDD.db`），保留 30 份；口径变更与表关系变更操作前后即时快照 |
| Parquet 业务数据 | 原始上传文件（`data/uploads/`）保留不清除，作为可重放来源；Parquet 可由原始文件重建 |
| 恢复演练 | 备份文件 + uploads 目录即可完整恢复系统（元数据 + 业务数据均为文件，恢复即拷回） |

### 10.3 运行日志

- 标准库 logging + 结构化格式（时间、级别、模块、trace_id），落 `data/logs/app.log`，按天轮转保留 30 天
- 关键事件强制记录：指标变更、权限变更、登录成功/失败、导出、**数据导入与表关系变更**、**预警触达结果与失败重试**
- `metric_changes` 为业务审计留痕，运行日志为技术排障，二者并存不可互替

---

## 十一、风险与缓解（架构相关）

| 风险 | 缓解 |
| --- | --- |
| DuckDB 写并发限制 | 单进程部署（D8）；阶段 1 起的导入操作安排在低峰执行，且经 pyarrow 写入而非 DuckDB 写连接；未来如需并发导入，引入写队列 |
| 指标编译器算子或关联覆盖不足 | v1 算子清单与星型关联限制明确（7.2），超范围定义保存即拒绝并明确报错，不产生半成品；缺关系时报错引导至 `/datasets` |
| **跨表口径定义不出（V1.1）** | `dataset_relations` 显式注册 + 前端可视化配置；模板库预置常见行业表关系建议，降低配置成本 |
| 指标定义质量参差导致编译失败 | 保存时强制编译校验（7.8） |
| 模板口径与实际业务不符 | 模板导入后即普通指标，支持修改；模板文件持续随版本迭代 |
| SQLite 单文件承载全部口径资产 | 每日备份 + 变更前后即时快照（10.2）；仓储抽象保留迁移 PostgreSQL 路径 |
| 单进程部署的性能上限 | 阶段 1 并发用户有限；瓶颈出现时优先垂直扩容，再评估多进程（需重构缓存为集中式） |
| **自动建模引擎不可行（V1.1）** | 预研前移至阶段 1 末（D16），提前 20 周暴露；预置"自动推荐 + 人工确认"降级方案；预研结论决定 4a 范围 |
| **预警触达不可用（V1.1）** | 多通道降级（企微 → 邮件 → 站内）；出网策略提前申请（10.1）；送达失败重试与告警；试运行期以点击/处理率验证真实有效性 |
| **关键依赖版本不可用（V1.1）** | 阶段 0 完成 Deep Agents / DuckDB / ECharts 6.1 / Vite 8 可用性验证；锁定版本并记录降级路径 |

---

## 十二、版本修订记录

### V1.2 → V1.3（2026-09-10，确认开工前 5 项决策 + 行业无关约束）

| # | 修订项 | 修订内容 |
| --- | --- | --- |
| 1 | 行业无关 | 新增**不变式 7**：平台不得内置行业语义，行业差异只允许通过数据层与配置层表达；新增 D21 与"跨行业一致性"测试 |
| 2 | 合成数据策略 | D21：生成 ≥2 套结构差异显著的数据集（零售交易型 + 订阅服务型），脚本置 `scripts/`；作为行业无关性的验证载体，非目标业务数据 |
| 3 | 涨跌配色定稿 | D19 由"待确认"转为**已确认**：状态色语义（绿=向好 / 红=恶化）+ 箭头 + 文字 |
| 4 | 后端运行时 | 新增 D22：Python 3.13.12（managed venv），源码保持 3.11+ 兼容；技术栈表同步 |
| 5 | 首批口径与运行方式 | 沿用模板库冷启动（D7）；本地 Windows 开发机运行、启动脚本化（见开发执行方案） |

### V1.1 → V1.2（2026-09-10，补齐前端规范与品牌标准）

| # | 修订项 | 修订内容 |
| --- | --- | --- |
| 1 | 前端规范落地 | D2 补全版本矩阵（Vue 3.5.34 / Vite 8.0.13 / Pinia 3.0.4 / Vue Router 5.0.7 / Element Plus 2.14.0 / Axios 1.16.1 / ECharts 6.1.0）与工程约束（精确补丁版本、npm 锁文件、四环境）；技术栈表同步 |
| 2 | UI 体系判定 | 新增 D18：判定为 Product UI regime，复用 `pwc-ui-skill` 的 Element Plus preset，不手写 token；字体、Logo、圆角、暗色模式、regime 标注要求 |
| 3 | 配色语义 | 新增 D19：指标涨跌默认用状态色（绿=向好 / 红=恶化）+ 箭头与文字；如需"红涨绿跌"则配置化反转。**待业务方确认** |
| 4 | 依赖与运行时 | 新增 D20：Node `^20.19.0 \|\| >=22.12.0`、npm、`type: module`、`build` ≡ `build:pro`、禁 `.env.production` |
| 5 | 品牌落地章节 | 新增 7.12：样式基座、字体、Logo、色彩、圆角、栅格、组件状态、图表、禁止项、留空表达与 10 项自检清单 |
| 6 | 页面与测试 | 7.7 补充页面级品牌要点与 primary surface 判定；测试策略前端行并入品牌自检清单 |

### V1.0 → V1.1（2026-09-10，依据可行性评审）

| # | 修订项 | 修订内容 |
| --- | --- | --- |
| 1 | 数据接入前移 | 新增 D12 与 7.9，`datasource` 模块阶段 1 启用最小子集；修正 7.3 中"写操作发生在阶段 2"的过期表述 |
| 2 | 数据集与表关系 | 新增 D13 与 7.1 三张表（`datasets`/`dataset_relations`/`dataset_coverage`）；7.2 补充 Join 表达与星型限制；不变式 6 禁止隐式推断 |
| 3 | 预警触达通道 | 新增 D14、不变式 5、7.10 与 `notify_log` 表；6.3 数据流补充触达环节；10.1 补充出网要求 |
| 4 | 指标层级与构成 | 新增 D15，`metrics` 增加 `level`/`parent_id`/`disambiguation`/`primary_dataset_id`；目录与归因复用 |
| 5 | 自动建模预研前移 | 新增 D16 与 7.11（待填写模板），并规定"预研结论未产出前阶段 4a 不得进入排期" |
| 6 | 理解卡默认算法可配置 | 新增 D17，`disambiguation` 字段 + 指标管理页配置入口 + 模板库预填建议 |
| 7 | 缓存键扩展 | 缓存键由"指标ID:ver"扩展为"指标ID:ver:dataset_ver"，覆盖数据变化场景（原为阶段 2 遗留必答项，现提前解决） |
| 8 | 阶段映射更新 | 3.1 与全局架构图按计划 V1.4 的 6 个阶段（含 4a/4b）更新 |
| 9 | 测试与风险 | 测试策略新增异动算法回归与触达送达测试；风险表新增跨表口径、自动建模、触达不可用、依赖版本四项 |
| 10 | 演进必答项 | 第九章增补阶段 2（增量导入、覆盖刷新、质检可操作修正）与阶段 3（基线算法、标注集、触达配置）必答项 |

---

*本文档为 V1.2 基线，阶段 2/3/4a 开工前补充各自详细设计并升版。7.11 自动建模预研结论待 M1a 后填写；D19 配色语义待业务方确认。*
