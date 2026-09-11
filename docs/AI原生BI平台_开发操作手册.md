# AI 原生 BI 平台 · 开发操作手册

> 用途：把常用命令、环境配置、常见报错处理、以及问过我的问题集中在这里，**不用再翻对话**。
> 编制：WorkBuddy Agent（vv）｜版本：V1.0｜日期：2026-09-10｜适用批次：B0 + S1（已交付）

---

## 一、环境与路径（先看这里）

| 项 | 值 |
|---|---|
| 项目代码根 | `C:\Users\William Y Liang\Desktop\工作内容\AI Agent\BI Platform_WorkBuddy\bi-platform` |
| Python 解释器（venv） | `C:\Users\William Y Liang\.workbuddy\binaries\python\envs\default\Scripts\python.exe` |
| Python 版本 | **3.13.14**（源码保持 3.11+ 兼容） |
| 后端目录 | `bi-platform/backend`（pytest / uvicorn 都以此为工作目录） |
| 数据目录（dev） | `bi-platform/data/`：`uploads/` `parquet/` `metadata.db` `synthetic/` |
| 进度文件 | 工作区根 `PROGRESS.md`（新会话先读它） |

目录结构：

```
bi-platform/
├── backend/           后端（FastAPI + SQLAlchemy + SQLite）
│   ├── app/           core（配置/响应/日志）· infra（数据库/仓储/存储/模型）· api（路由）
│   ├── env/           .env.dev / .env.test / .env.uat / .env.pro
│   ├── tests/         测试（当前 18 项）
│   └── pytest.ini
├── scripts/           gen_synthetic_data.py、bi.sh（Git Bash）、bi.ps1（PowerShell）
└── data/              运行数据（已 gitignore，不入库）
```

---

## 二、常用命令速查

### 1. 一键脚本（推荐）

| 目的 | PowerShell / PyCharm 终端 | Git Bash |
|---|---|---|
| 跑测试 | `.\scripts\bi.ps1 test` | `./scripts/bi.sh test` |
| 起服务（热重载） | `.\scripts\bi.ps1 run -Env dev -Port 8100` | `./scripts/bi.sh run dev 8100` |
| 重新生成合成数据 | `.\scripts\bi.ps1 seed -Rows 100000` | `./scripts/bi.sh seed 100000` |

切环境：把 `dev` 换成 `test` / `uat` / `pro`（对应 `backend/env/.env.{环境}`，数据目录随之切换）。

### 2. 等价的原生命令（脚本出问题时用）

```powershell
# 跑测试
$env:APP_ENV="test"; cd bi-platform\backend
& "C:\Users\William Y Liang\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m pytest -v

# 起服务
$env:APP_ENV="dev"; cd bi-platform\backend
& "C:\Users\William Y Liang\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8100
```

### 3. 服务运维

| 目的 | 命令 |
|---|---|
| 停止服务 | 服务窗口按 `Ctrl+C` |
| 端口被占用 | `netstat -ano \| findstr :8100` → `taskkill /PID <pid> /F` |
| 健康检查 | 浏览器 `http://127.0.0.1:8100/api/health`，或 `curl http://127.0.0.1:8100/api/health` |
| 接口调试 | 浏览器 `http://127.0.0.1:8100/docs`（Swagger）；`/` 会自动跳转到这里 |
| 查元数据库表 | `python -c "import sqlite3;print([r[0] for r in sqlite3.connect(r'bi-platform/data/metadata.db').execute(\"select name from sqlite_master where type='table'\")])"` |
| 重置 dev 库 | 停服务 → 删除 `bi-platform/data/metadata.db` → 重启（生命周期自动建表） |

### 4. 合成数据

```bash
python scripts/gen_synthetic_data.py --dataset both --rows 100000      # 默认，各 10 万行
python scripts/gen_synthetic_data.py --dataset a --rows 1000000        # 压测：单套 100 万行
python scripts/gen_synthetic_data.py --dataset b --rows 50000 --seed 7 # 换随机种子
```

产物：`data/synthetic/{dataset_a,dataset_b}/` + `manifest.json`（记录行数、编码、混杂列、异常值、表关系）。

### 5. 上传数据（B1 起，服务运行时）

服务起着的前提下（`http://127.0.0.1:8100`）：

```bash
# 上传（name 可选，缺省取文件名；重名自动加 _2）
curl -X POST http://127.0.0.1:8100/api/datasets/upload -F "file=@你的文件.csv" -F "name=orders"

# 列表 / 详情 / 预览前 20 行
curl http://127.0.0.1:8100/api/datasets
curl http://127.0.0.1:8100/api/datasets/9
curl "http://127.0.0.1:8100/api/datasets/9/preview?rows=20"

# 查混杂列的异常值定位（行号+值+类型）
curl "http://127.0.0.1:8100/api/datasets/9/columns/coupon_code/anomalies?limit=20"

# 注册表关系（显式声明，禁止隐式推断）
curl -X POST http://127.0.0.1:8100/api/datasets/9/relations -H "Content-Type: application/json" \
  -d '{"from_column":"user_id","target_dataset_id":2,"target_column":"user_id","relation_type":"many_to_one"}'

# 删除数据集（级联删除关系、覆盖区间、文件）
curl -X DELETE http://127.0.0.1:8100/api/datasets/9
```

不习惯 curl：打开 `http://127.0.0.1:8100/docs`，在 Swagger 页面上传与调试更直观。

### 6. 指标管理（B2 起，服务运行时）

```bash
# 试编译（不落库，先看 SQL 长什么样）
curl -X POST http://127.0.0.1:8100/api/metrics/compile -H "Content-Type: application/json" \
  -d '{"calc_rule":{"base_aggregation":"sum","source":{"table":"orders","column":"pay_amount","filter":"order_status = '\''paid'\''"}}}'

# 创建指标（编译不过=超纲算子/缺表关系/列不存在，保存即拒绝并说明原因）
curl -X POST http://127.0.0.1:8100/api/metrics -H "Content-Type: application/json" \
  -d '{"code":"gmv_paid","name":"支付GMV","aliases":["GMV","销售额"],"calc_rule":{"base_aggregation":"sum","source":{"table":"orders","column":"pay_amount","filter":"order_status = '\''paid'\''"}},"topic":"trade"}'

# 目录/搜索（search 同时命中名称、code、别名）/ 详情
curl "http://127.0.0.1:8100/api/metrics?search=GMV"
curl http://127.0.0.1:8100/api/metrics/1

# 改口径（calc_rule 变更必须带 reason → 自动重编译、ver+1、写存档、留痕）
curl -X PATCH http://127.0.0.1:8100/api/metrics/1 -H "Content-Type: application/json" \
  -d '{"calc_rule":{...新规则...},"reason":"口径评审结论","operator_id":"liang"}'

# 当前版编译存档 SQL / 变更历史
curl http://127.0.0.1:8100/api/metrics/1/sql
curl http://127.0.0.1:8100/api/metrics/1/changes

# 删除（软删除：目录与搜索不可见，留痕保留）
curl -X DELETE http://127.0.0.1:8100/api/metrics/1
```

dev 库已内置 3 个示例指标可试手：`gmv_paid`（支付GMV）、`avg_ticket`（客单价，比率类）、`mrr_active`（活跃MRR，订阅数据集 B，演示 time_field 用法）。filter 中的状态值已对齐合成数据（订单状态为中文：已支付/已退款/部分退款）。

---

### 7. 登录与权限（B4 起，必须先登录才能调接口）

```bash
# 登录拿 token（dev 引导账号 admin/admin123；启动时 users 表为空会自动创建）
curl -X POST http://127.0.0.1:8100/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'
# → data.token，后续所有请求带 -H "Authorization: Bearer <token>"

# 当前登录用户 / 用户管理（仅 admin）
curl http://127.0.0.1:8100/api/auth/me -H "Authorization: Bearer $TOKEN"
curl -X POST http://127.0.0.1:8100/api/auth/users \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username":"zhang","password":"xxxxxx","role":"analyst","department":"数据部"}'
# role ∈ admin（全部权限）/ analyst（建指标改口径）/ viewer（只读）

# 指标可见性登记（仅 admin；默认可见，登记后对目标角色/部门隐藏——
# 目录与取数两出口同时生效；items=[] 清空即恢复全员可见）
curl -X PUT http://127.0.0.1:8100/api/auth/metrics/1/restrictions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"items":[{"subject_type":"role","subject_value":"viewer"}]}'
```

Swagger 调试方式：`/docs` 页右上角 **Authorize** 按钮，粘贴 `Bearer <token>`（含 Bearer 前缀）后所有接口自动带 token。

### 8. 统一取数（B3 起，服务运行时）——看板/问数/报告以后都走这里

```bash
# 指标单值（metric 兼容 id 和 code；compare 可选 none/mom/yoy）
curl -X POST http://127.0.0.1:8100/api/query/metric-value \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"metric":"gmv_paid","start":"2025-01-01","end":"2025-12-31","compare":"yoy"}'

# 返回关键字段：value（null 需看 period_complete 区分语义）、
#   period_complete=false → 查询范围超出数据覆盖区间（前端显示"—"）
#   period_complete=true 且 value=null → 区间无数据或比率分母为 0
#   cache: miss/hit/bypass（不完整周期 bypass，不缓存）

# 导出按日序列 CSV（UTF-8 BOM，Excel 直接打开；单次最多 3 年）
curl -X POST http://127.0.0.1:8100/api/query/export \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"metric":"avg_ticket","start":"2026-01-01","end":"2026-01-31"}' \
  -o avg_ticket_jan.csv
```

---

## 三、常见报错与处理（踩过的坑）

| 现象 | 原因 | 处理 |
|---|---|---|
| 双击/运行 `.sh` 弹出"选择打开方式" | Windows 不认识 `.sh`，它是 Git Bash 脚本 | PowerShell 里改用 `.\scripts\bi.ps1`；或切到 Git Bash 终端 |
| `禁止运行脚本` / ExecutionPolicy | PowerShell 执行策略限制 | `powershell -ExecutionPolicy Bypass -File .\scripts\bi.ps1 test`（单次）；或 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`（永久） |
| 中文输出乱码，如 `鍚姩鍚庣` | Windows PowerShell 5.1 按 GBK 读无 BOM 的 UTF-8 脚本 | 已修（`bi.ps1` 改为 UTF-8 with BOM + 控制台编码设置）；仍乱码则先执行 `chcp 65001` |
| 访问 `http://127.0.0.1:8100/` 显示 404 | 根路径未定义（已修） | 已修：自动跳转 `/docs`；仍 404 说明是旧进程，重启服务 |
| `GET /favicon.ico 404` | 浏览器自动请求图标 | 无害，忽略 |
| `StarletteDeprecationWarning: install httpx2` | Starlette 前瞻提示 | 无害，不影响 `18 passed`；将来 Starlette 移除 httpx 支持时再迁移 |
| `metadata.db` 为 0 字节 | 建表静默失败（已修） | 已修并加回归测试；若复现，删除该库重启服务即可重建 |
| PyCharm 里 import 报红 | 未标记源码根 | 右键 `bi-platform/backend` → Mark Directory as → Sources Root；解释器指向上面的 venv |

---

## 四、PyCharm 配置（边改边调用）

| 配置项 | 值 |
|---|---|
| 解释器 | Settings → Python Interpreter → Add → Existing → 上面 venv 的 `python.exe` |
| 起服务 | Run Config：Module `uvicorn`；Parameters `app.main:app --reload --host 127.0.0.1 --port 8100`；Working dir `bi-platform/backend`；Env `APP_ENV=dev` |
| 跑测试 | Python tests → pytest；Target `bi-platform/backend/tests`；Working dir `bi-platform/backend`；Env `APP_ENV=test` |
| 消红线 | 右键 `backend` → Mark Directory as → Sources Root |

---

## 五、接口契约速查（前后端共同遵守）

**响应体**：`{code, message, data}`；HTTP 状态码与 `code` 前三位对齐（D11）。

| body code | HTTP | 含义 | 前端行为 |
|---|---|---|---|
| 0 | 200 | 成功 | 解包 `data` |
| 40000 | 400 | 参数错误 | 提示 |
| 40100 | 401 | 未认证 | 跳登录 |
| 40300 | 403 | 无权限 | 仅提示 |
| 40400 | 404 | 不存在 | 提示 |
| 40900 | 409 | 冲突 | 提示 |
| 50000 | 500 | 服务内部错误 | 提示 |

**现有接口**：

| 接口 | 说明 |
|---|---|
| `GET /api/health` | 返回 `{status, app, version, env, database, time}`；db 异常返回 500/50000 |
| `GET /` | 跳 `/docs` |
| `GET /docs` | Swagger 调试页 |
| `POST /api/datasets/upload` | 上传 CSV/XLSX（multipart，`file` 必填 + `name` 可选）→ 返回数据集详情；重名自动 `_2`；列数不一致返回 400 + 异常行号 |
| `GET /api/datasets` | 数据集清单 |
| `GET /api/datasets/{id}` | 详情：列 schema（类型/mixed/null 数/样本）+ 覆盖区间 |
| `PATCH /api/datasets/{id}/name` | 重命名（仅元数据标签，不动文件）；重名返回 409。**Swagger 表单 name 忘改占位符 `string` 时用这个补救** |
| `GET /api/datasets/{id}/preview?rows=20` | 回读 Parquet 前 N 行（上限 200） |
| `GET /api/datasets/{id}/columns/{col}/anomalies?limit=50` | 混杂列异常值定位（行号 + 值 + 实际类型） |
| `POST /api/datasets/{id}/relations` | 注册表关系（from_column / target_dataset_id / target_column / relation_type） |
| `GET /api/datasets/{id}/relations` | 关系列表 |
| `DELETE /api/datasets/{id}/relations/{rid}` | 删除关系 |
| `DELETE /api/datasets/{id}` | 删除数据集（级联关系/覆盖区间/文件） |

**列类型语义**：`int / float / date / datetime / bool / string / mixed`；int+float 混合统一为 float（精度差异不算混杂）；date+datetime 混合统一为 datetime；`yyyy-MM-dd HH:MM:SS` 识别为 datetime 并自动登记覆盖区间；前导零数字（`007`）保留为 string（保护编码类 ID）；空串与字面量 `NULL`（大小写不敏感，ETL 导出常见）→ null（`N/A`/`NA` 可能是业务码，保留 string）。

**业务异常**：路由内 `raise BusinessError("提示", BodyCode.BAD_REQUEST, data=可选载荷)`，全局处理器转统一响应。

---

## 六、合成数据说明（B1 联调的输入）

| 数据集 | 形态 | 表 | 特征 |
|---|---|---|---|
| A | 零售交易型 | orders / users / products / channels | 订单粒度、退款、折扣、品类×区域；**orders.csv 为 GBK** |
| B | 订阅服务型 | subscriptions / accounts / plans / usage_events | 订阅周期、经常性收入、账户粒度，**无"订单"概念**；**accounts.csv 为 GBK** |

质检靶点（B1 要识别出来的东西）：

- **编码**：每套各 1 个 GBK 文件（记事本打开显示乱码 = 正常）
- **混杂类型列**：`users.external_ref`、`orders.coupon_code`、`accounts.crm_ref`、`usage_events.response_ms`（数值/字符串/空值混排）
- **异常值**：`quantity=0`、负实付、退款>实付、负 MRR、`response_ms="超时"`（5083 处）
- **覆盖区间**：2025-01-01 ~ 2026-09-09，**9 月天然不完整** → 用于 B3「不完整周期返回 null 而非 0」验证

---

## 七、问答沉淀（问过我的问题）

| 问题 | 结论 |
|---|---|
| 自己怎么测试验证？ | 三种方式：① 终端脚本 ② PyCharm ③ 让我在 WorkBuddy 跑（说"跑测试/起服务冒烟"即可）。我不能替你点浏览器页面，前端视觉走查需你本机打开 |
| 用 PyCharm 还是在 WorkBuddy？ | 都行：自动化回归交给我，交互式调试/看页面交给 PyCharm 或终端 |
| `.sh` 在 PowerShell 弹出打开方式？ | `.sh` 给 Git Bash 用；PowerShell 用 `bi.ps1` |
| 测试结果有问题吗？ | 没问题；唯一的 `httpx2` 警告是前瞻提示，不影响 |
| 启动日志里的 404 正常吗？ | `/favicon.ico` 无害；`GET /` 404 已修为跳转 `/docs` |
| 中文乱码怎么办？ | 见第三节；`bi.ps1` 已加 BOM，必要时 `chcp 65001` |
| 改了代码没生效？ | **不要依赖 `--reload` 热重载验收**：曾出现改动后旧进程继续服务。改代码后 `Ctrl+C` 重启服务再验证 |
| 上传报 500 怎么排查？ | 响应体 `data` 里带 `{exception, detail}`（真实原因），把它发我即可；完整堆栈在服务端 PowerShell 日志。已防护的常见场景：数值超 int64 → 400、文件被 Excel 占用 → 400 |
| 报 UnicodeDecodeError / 编码问题？ | **已自动处理**：采样识别只看文件头部 64KB，若文件"前段纯英文+后段中文 GBK"会被误判成 utf-8，现在解码失败会自动换 GBK/GB18030 整文件重试，响应里返回实际编码。全失败才报 400（提示另存为 UTF-8） |
| 删除数据集后 uploads/parquet 还有文件？ | 响应里看 `files_failed`：文件被占用时清理会失败并记录路径，关闭占用程序后按路径手动删即可 |
| 想上传自己的文件试试？ | 服务起着，Swagger（`/docs`）里 POST `/api/datasets/upload` 直接传，或用第二节第 5 条的 curl；CSV/XLSX、GBK/UTF-8 都行 |
| discount 为什么不是"混杂"？ | `0` 和 `0.05` 同列只是精度差异（int+float → 统一 float）；混杂专指真正的类型冲突（如数值+文字） |
| Swagger 上传后名字变成了 "string"？ | Swagger 表单里 `name` 字段的默认占位值就是 `string`，没改就直接传入成了真名。已清理脏数据并重新接入为 `bkg_ghost`；以后可用 `PATCH /api/datasets/{id}/name` 改名，无需重传 |
| 为什么我传的文件覆盖区间（coverage）是空的？ | 旧版只识别纯日期 `yyyy-MM-dd`，你文件里的 `2023-08-21 08:30:00` 是 datetime，没被识别。已修复：datetime 列也会登记覆盖区间（如 BKG_START 2023-04-03 ~ 2026-08-28） |
| ETL_DATE 列全是 "NULL" 字符串？ | ETL 导出的字面量 `NULL` 现在按空值处理（null_count 计入）；`N/A`/`NA` 可能是业务码，仍保留为字符串 |
| BKG_GHOST 列全空正常吗？ | 该列 2804/2804 全空，接入层如实记录 null_count，不删列；是否保留由你决定（可能是源系统遗留字段） |
| 想定义一个指标但不知道 calc_rule 怎么写？ | 两种形态见 PROGRESS.md「calc_rule 语义」；最直接的办法：先 `POST /api/metrics/compile` 试编译（不落库），报错信息会告诉你哪里不合法 |
| 为什么创建指标报"缺少 X → Y 的表关系"？ | 跨表过滤列必须走显式登记的表关系（不变式 6），先在数据集管理里登记关系再建指标 |
| 比率类指标分母为 0 会报错吗？ | 不会，编译产物对除法包了 NULLIF，分母 0 返回 NULL（B3 前端显示"—"） |
| 改了口径旧数据会混吗？ | 不会。改 calc_rule 必须 reason，一次请求内原子完成：重编译 → 存档 → ver+1 → 留痕；旧版本存档仍可回看 |
| 元数据库里的 created_at 比当前时间慢 8 小时？ | 曾因 B0-B3 用 UTC 入库导致（梁老师 2026-09-10 发现），已修复：现在统一存**本地时间**，dev 库存量记录已批量 +8h 校正；若你看到旧数据仍是慢 8 小时，那是修复前入库的，可忽略或反馈批量校正 |
| 取数返回 null 是算错了吗？ | 先看 `period_complete`：false = 查询范围超出数据覆盖区间（如查 2027 年但数据只到 2026-09）；true = 区间无数据或比率分母为 0。都属正常语义，前端显示"—" |
| 查出来一直是 null 但数据明明有？ | 八成是 filter 字面量和实际数据对不上（如写 `'paid'` 但数据是 `'已支付'`）。用 `GET /api/datasets/{id}/columns/{col}/anomalies` 看枚举分布核对 |
| 为什么同样的查询第二次快？ | LRU 缓存命中（响应里 `cache: "hit"`）；改口径或数据重导入后版本号变化，旧缓存自动失效，无需手动清 |
| 导出 CSV 打开中文乱码？ | 不会——已带 UTF-8 BOM，Excel 直接双击打开即可；日期为行、列为 date/value/period_complete |
| 导出报"范围过长"？ | 单次导出上限 3 年（1095 天），分批导出即可 |
| 接口突然全返回 401 了？ | B4 起所有接口需登录。先 `POST /api/auth/login`（dev 引导账号 admin/admin123）拿 token，之后每个请求带 `Authorization: Bearer <token>`；Swagger 用页面右上角 Authorize 按钮粘贴 `Bearer <token>` |
| 登录报"用户名或密码错误"但账号确实存在？ | 密码错或账号被停用（停用会提示"账号已停用"）；admin 可在 `GET /api/auth/users` 里核对账号状态 |
| 401 和 403 什么区别？ | 401=没登录/token 无效过期（去登录）；403=已登录但无权限（viewer 想写操作、访问被限制的指标），找 admin 提权或核对可见性登记 |
| 受限指标对用户完全隐身吗？ | 是。可见性登记后，无权限用户在目录/搜索里看不到，详情/SQL/历史/取数/导出全部 403，名称与口径不泄露；admin 恒全量可见 |
| token 会过期吗？ | 默认 12 小时（`jwt_expire_minutes` 可配）；改密码或停用账号后旧 token 立即失效（每请求回库校验） |
| 忘记 admin 密码怎么办？ | dev 环境最简单：删掉 `data/metadata.db` 里 users 表该行（或整库重建）后重启服务，引导逻辑会按配置重建 admin；pro 环境用 `PATCH /api/auth/users/{id}` 改密码 |

---

## 八、当前状态与下一步

**已交付**：B0 脚手架 + S1 合成数据 + B1 数据接入 + B2 指标元数据 + 编译器 + B3 统一计算服务 query + B4 auth 与权限双出口 + B5 行业指标模板库（4 行业 YAML 101 条、幂等导入、pytest **176 passed**）+ **B6 前端主体**（`bi-platform/web/`：Vue 3.5 + Vite 8 + Element Plus 2.14 + ECharts 6 + PwC 品牌基座；六页面：登录/指标目录/指标详情/统一看板/指标管理/数据集与表关系；四环境 build 全通过；浏览器走查 console 0 错误）。

**前端启动**：`cd bi-platform\web` → `npm run dev`（Node ≥22.12）→ `http://localhost:5173`（Vite 代理 `/api` → 8100）。后端照旧 8100，登录 admin/admin123。

**下一批 B7**：MVP 联调与验收——端到端场景串测（上传→建关系→导模板→建指标→看板→导出→权限三角色），修复联调问题并出验收清单。

**B5 冒烟**：重启服务后运行 `backend\scripts\smoke_b5.py`（可传端口参数），9 步断言幂等可重复。

---

*本手册随批次推进更新；内容以 `PROGRESS.md` 的当前状态为准。*
