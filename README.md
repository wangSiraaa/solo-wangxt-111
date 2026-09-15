# 离线生料配比试算工作台

> ⚠️ **虚构工艺边界**：本项目全部原料成分、含水率、价格、可供量与率值区间均为虚构数据，
> **仅供离线工艺研究与方法验证，不构成对任何真实生产设备的操作指令**。
> 输出结果不得直接用于实际配料下井、入磨或窑系统控制。

比较**原料成本**与生料**化学指标（KH 石灰饱和系数 / SM 硅率 / IM 铝率）**之间的取舍：

- **Angular 19** 前端：展示各氧化物的**原料来源与比例**、干/湿基配比、成本构成、多方案对比与完整追溯链路；
- **FastAPI** 后端：先按**质量守恒**合成生料，再计算率值，调用 **SciPy（SLSQP + HiGHS linprog）** 做约束优化与可行性诊断；
- **PostgreSQL**：存储检测成分（化验版本，区分 NULL 缺测与 0.00 未检出）、含水率、到厂成本、可用量与每次求解的完整 trace。

## 计算口径（在 UI 页脚与每个结果的 trace 中同样可查）

| 项目 | 公式 / 约定 |
|---|---|
| 决策变量 | xᵢ = 原料 i 占**干生料**质量分数，Σxᵢ = 1 |
| 质量守恒合成 | 合成成分 = Σ xᵢ·cᵢ（cᵢ 为该原料干基质量百分数） |
| 硅率 SM | SiO₂ / (Al₂O₃ + Fe₂O₃) |
| 铝率 IM | Al₂O₃ / Fe₂O₃ |
| 石灰饱和系数 KH | (CaO − 1.65·Al₂O₃ − 0.35·Fe₂O₃) / (2.8·SiO₂) |
| 碱当量 | Na₂O + 0.658·K₂O |
| 干湿基换算 | 湿吨 = 干吨 /（1 − 含水率）；干基吨成本 = 湿吨价 /（1 − 含水率） |
| 灼烧基 | 合成成分 × 100/(100 − LOI) |

### 硬性报错策略（绝不默认零含量）

1. **缺测**：CaO/SiO₂/Al₂O₃/Fe₂O₃/LOI 任一为 NULL → `MISSING_ANALYTES` 422，列出原料×项目，拒绝参与合成或优化；
2. **分母保护**：合成 Fe₂O₃、Al₂O₃+Fe₂O₃、2.8·SiO₂ 低于 `denom_floor`（默认 0.05%）→ `ZERO_DENOMINATOR`，
   不产生 inf/NaN；优化器侧以同值线性地板约束在可行性阶段拦截；
3. 含水率不在 [0,100%)、配比和 ≠ 1、最低掺量 > 可用量上限、最低掺量之和 > 100% 等均显式报错。

## 约束体系

- 率值 KH/SM/IM 目标区间（线性分式 → 齐次线性约束，分母由地板约束保证严格为正）；
- 有害组分上限：MgO、SO₃、碱当量、Cl⁻；
- 原料**最低掺量**、场景级上限与**可用量**（可供量折算配比上限，二者取小）；
- 求解失败时给出冲突诊断：
  - 单项放松可恢复 → 点名该约束，并给出该率值在结构约束下的**二分搜索可达区间**；
  - 成对冲突 → 给出冲突对；
  - 多约束联合冲突 → **最小总违约弹性规划**（Σslack 最小的 LP）列出在最近违约配比下实际违反的每条约束、
    实际率值/组分与边界、缺口，以及该“最近配比”本身（仅供定位，不是可行解）。

## 三种方案偏好（同一边界下展示多可行解）

1. 💰 `min_cost` 干基吨成本最低；
2. 🎯 `target_center` 三个率值尽量居目标带中心；
3. 🏷 `max_cheap` 标记为廉价的原料占比最大化（成本作为同值时的次级目标）。

## 内置演示场景（虚构）

| 场景 | 演示点 | 预期 |
|---|---|---|
| S1 六原料常规配比 | **含水率差异**：旱季 vs 雨季（粉煤灰 18%→26% 等 + 低钙石灰石雨季附加成本） | 两侧均可行，雨季成本上升、湿料用量增加，方案对比表逐项给出变化 |
| S2 廉价原料强配 | **廉价原料导致指标超限**：低钙石灰石 ≥35% + 煤矸石 ≥15%，KH ≥0.90 且碱当量 ≤0.6% | 不可行，诊断出 KH 下限（实际 0.046）与碱当量上限（实际 1.25%）、Cl⁻ 上限联合冲突 |
| S3 缺测原料 | 新矿点砂岩 Fe₂O₃ 缺测（NULL） | 422 `MISSING_ANALYTES`，明确到原料与项目 |
| S4 未检出铁原料 | 石英砂 Fe₂O₃ = 0.00%（显式未检出，非缺测）+ 96% 强配 | 不可行，地板约束「IM 分母 Fe₂O₃ ≥ 0.05%」被点名（实际 0.024%） |

另在「手工配比试算」页可用滑块复现零分母报错与雨季含水率覆盖。

## 可追溯性

每个可行解的 `trace_json` 都包含：

- `provenance.assay_versions`：每种原料生效的**化验版本号/版本 ID/实验室备注**、湿吨价、最低/上限；
  NULL 与 0.00 在前端分别以红色「缺测」和灰色「0.00」区分；
- 质量守恒合成明细：每原料对每一氧化物的质量贡献、干基/灼烧基合成值；
- 干湿基换算：含水率、换算系数、湿料质量、湿基配比、干基价与成本贡献、1000 kg 干生料的水平衡；
- 率值分母三项校验值与阈值；
- 优化器使用的全部线性约束系数矩阵（可手工复核）。

历史结果可通过 `GET /api/scenarios/{id}/solutions` 与 `GET /api/solutions/{id}` 回溯。

## 自定义场景与修订版（草稿—发布冻结—可重放审计）

研发对同一新矿点持续调边界时，历史解**绝不被后续修改覆盖**：

- **建单即发布 rev1**：`POST /api/scenarios` 单事务创建场景 + 已发布修订；
- **修订链**：`PUT /scenarios/{id}/draft`（每场景至多一个草稿，递增 revision_no、lock_version）→
  `POST /scenarios/{id}/publish`（原子冻结并同步当前求解快照）；
  旧发布修订与旧解永久可读，**已发布数据不可直接改写**（`PUT /scenarios/{id}` 返回 405）；
- **只有已发布修订可求解**（草稿返回 409）；求解可带 `?revision_no=N` **重放任意旧发布修订**，
  每个 Solution 绑定 `scenario_revision_id/revision_no`，历史列表按修订关联；
- **化验版本钉住**：修订 payload 记录每个原料当时的 `assay_id/cost_id`，
  切换生效化验后旧修订、旧解仍引用旧版本，新修订、新解才使用新版本；
- **乐观并发**：草稿保存/发布必须携带 `lock_version`，服务端用带版本条件的 UPDATE 保证
  两个浏览器并发提交只有一个成功，另一个收到 409；
- **幂等请求键**：`Idempotency-Key` 头——重复提交返回同一修订结果（`replay:true`），
  同键不同内容返回 409；
- **无半成品**：发布是单事务（状态翻转 + 场景快照 + 幂等日志），失败/重启整体回滚；
  启动 `recover()` 修复孤儿发布指针、并为升级前旧场景回填 rev1；
- **回滚**：`POST /scenarios/{id}/rollback-draft` 把任意旧发布版复制为新草稿
  （记录 `created_from_revision_no`，审计链完整，历史冲突诊断不受影响）；
- **内置保护**：S1–S4 有冻结 rev1，草稿/发布/回滚接口一律 403。

Angular 场景栏显示当前发布版、草稿状态与 lock；「版本时间线」面板列出每个修订的状态、来源、
时间、关联解数、与发布版的**差异摘要**（边界/雨季/原料增删与掺量、化验版本变化），
并提供「重放求解 / 复制为新草稿 / 继续编辑 / 发布 / 放弃」；历史解表新增修订列。

### 修订相关接口

| 方法/路径 | 说明 |
|---|---|
| `GET /api/scenarios/{id}/revisions[/{no}]` | 版本时间线（草稿含差异摘要）/ 单修订 |
| `PUT /api/scenarios/{id}/draft` | 保存草稿（body 带 `lock_version`/`source_revision_no`，头带 `Idempotency-Key`） |
| `POST /api/scenarios/{id}/publish` | 发布草稿（乐观锁 + 幂等键；发布前重跑全量校验） |
| `POST /api/scenarios/{id}/rollback-draft` | 旧发布版复制为新草稿 |
| `DELETE /api/scenarios/{id}/draft` | 放弃草稿（不影响已发布版与历史解） |
| `POST /api/scenarios/{id}/solve?revision_no=N` | 重放指定已发布修订 |

## 自定义场景校验

「＋ 新建自定义场景」支持：选择参与原料、名称与说明、KH/SM/IM 目标区间、有害组分上限、
分母地板、每原料最低掺量/场景上限/廉价标记，以及可选的雨季含水率覆盖与雨季附加成本。

- **持久化**：保存后与内置场景完全同构，可执行旱季/雨季求解、看三种方案与冲突诊断，
  并从场景页「查看历史解」面板追溯每次求解（含快照化验版本号/ID）。
- **校验规则（前后端双重；服务端任何错误都在写库前返回，不落半成品）**：
  名称必填且**同名拒绝**；至少两种原料；原料编码必须存在且具有**生效化验与生效成本**
  （两者同时缺失时分别返回 `materials[<code>].assay` 与 `materials[<code>].cost` 两个可定位原因）；
  生效化验的必需氧化物不得缺测；率值下限严格小于上限且落在物理合理区间；
  有害上限、分母地板为正；**雨季含水率覆盖与附加成本的编码必须属于本次已选原料**
  （未知编码分别按 `rain_overrides.<code>` / `rain_extra_cost.<code>` 返回字段错误），
  含水率 ∈ [0,100%)、附加成本非负；
  最低掺量/场景上限 ∈ [0,100]%，最低 ≤ min(场景上限, 可用量上限)；
  **最低掺量之和 ≤ 100%**。错误按**字段名**返回（如 `materials[FA].min_pct`、
  `materials.min_sum`），表单逐字段展示。
- **内置保护**：S1–S4 标记 `built_in=true`，修改返回 403、删除返回 403，界面不显示编辑/删除入口。
- **化验版本快照**：切换某原料的生效化验后，**旧解仍引用并显示旧化验版本**，
  新求解使用新版本；历史面板与每个 trace 的 `assay_versions` 都可核对差异。

## 运行方式

### 方式一：Docker Compose（PostgreSQL + 后端 + 前端/nginx）

```bash
docker compose up --build
# 前端 http://localhost:8080   API 文档 http://localhost:8000/docs
```

首次启动自动建表并写入虚构演示数据。

### 方式二：本地开发（无需 Docker / PostgreSQL，用 SQLite 离线运行）

```bash
# 后端
cd backend
python3 -m pip install -r requirements.txt
BATCH_DATABASE_URL="sqlite:///./dev.db" uvicorn app.main:app --reload --port 8000

# 前端（另开终端，自带 /api 代理到 8000）
cd frontend
npm install
npm start    # http://localhost:4200
```

### 测试

```bash
cd backend && python3 -m pytest          # 25 个测试：化学计算/优化器/HTTP 链路
cd frontend && npm run build             # AOT 模板类型检查 + 生产构建
node smoke.cjs                            # Playwright 端到端冒烟（需先启动前后端）
```

## 主要 API

| 方法/路径 | 说明 |
|---|---|
| `GET /api/materials` · `/api/materials/{code}/assays` · `.../costs` | 原料、化验版本、成本 |
| `POST /api/materials/{code}/assays` · `.../activate` | 新增/切换生效化验版本（字段留空即 NULL 缺测） |
| `PUT /api/materials/{code}/availability` | 设置可用量配比上限（-1 不限） |
| `GET /api/scenarios` · `POST /api/scenarios/{id}/solve?profile=base|rain` | 场景列表 / 求解 |
| `POST /api/scenarios` · `PUT /api/scenarios/{id}` · `DELETE /api/scenarios/{id}` | 自建场景 CRUD（内置场景 403；非法输入按字段返回 422 且不写库） |
| `GET /api/materials/{code}/availability` | 可用量配比上限 |
| `POST /api/manual-blend` | 手工配比即时合成（支持含水率覆盖与附加成本） |
| `GET /api/scenarios/{id}/solutions` · `GET /api/solutions/{id}` | 历史结果追溯 |

## 目录

```
backend/app/
  chemistry.py   质量守恒合成、率值与分母保护、干湿基换算（纯函数）
  optimizer.py   SLSQP 多起点优化 + HiGHS 可行性/冲突诊断（弹性规划）
  models.py      SQLAlchemy 模型（化验版本/成本/可用量/场景/结果 trace）
  service.py     ORM→优化器编排、结果持久化与 provenance 组装
  seed.py        虚构演示数据
  main.py        FastAPI 路由
frontend/src/app/
  components/    solver（场景与方案对比）、manual（手工试算）、materials（原料库/版本）、
                 mix-result（氧化物来源/比例与追溯）、conflicts（冲突诊断）
```
