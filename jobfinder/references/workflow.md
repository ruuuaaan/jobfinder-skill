# 运行控制器协议（workflow-1.1）

适用版本：Skill 0.5.0 起。控制器实现见 `scripts/runtime/jobfinder/workflow.py` 与 `platform_auth.py`。
本协议只约束**经过本项目入口**的动作，边界见文末。

## 1. 运行状态

每次新检索创建一个 `run_id`，落盘在 `<workspace>/data/runs/<run_id>/`：

| 文件 | 内容 |
|---|---|
| `state.json` | 执行协议版本、计划版本、配置指纹、范围（城市/岗位/渠道/阶段顺序）、当前阶段、任务状态、预算、登录记录、范围调整、派生任务关系 |
| `events.jsonl` | 逐条动作事件（`search` / `auth_open` / `auth_probe` / `skip` / `blocked` / `close_stage` / `scope_adjust`） |
| `completion.json` | `finalize` 的整轮判定 |
| `auth/*.html` | 登录核验提交的页面快照（只能放在本目录下） |

`state.json` 摘要：

```json
{
  "run_id": "run-xxxx", "protocol_version": "workflow-1",
  "plan_id": "...", "config_fingerprint": "...",
  "scope": {"cities": ["上海"], "roles": ["财务BP"], "channels": ["official_career","51job","bendibao_web","bendibao_wechat"],
            "stage_order": ["official","recruitment","campus","bendibao_web","public_accounts"],
            "platform_auth_policy": "required", "user_scope_explicit": false},
  "current_stage": "official",
  "stages": {"official": {"status": "pending", "closed_at": null}},
  "tasks": [{"task_id":"上海-1","channel":"official_career","stage":"official","route":"direct",
             "status":"pending","attempts":0,"executed_at":null,"candidate_count":0,"evidence_ref":null}],
  "auth": {"51job": {"run_id":"run-xxxx","channel":"51job","browser_context_id":"ctx-1","tab_id":"tab-1",
                     "checked_at":"...","status":"verified","evidence_ref":".../auth/51job.html",
                     "dom_hash":"...","ttl_minutes":120}},
  "budget_used": {"searches": {"上海": 3}},
  "derived_tasks": [], "scope_adjustments": []
}
```

## 2. 命令

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `channels` | 输出渠道菜单（型号、阶段、能力差异），供用户勾选 | `[--config] [--output]` |
| `start-run` | 冻结本轮范围、创建运行 | `--config --plan (--channel ... 或 --all-channels) [--public-only] [--run-id]` |
| `next-action` | 返回当前唯一合法的下一动作 | `--run` |
| `record-action` | 提交实际执行回执 | `--run --input <JSON>` |
| `close-stage` | 阶段内任务都有处置后关闭阶段 | `[--stage] --run` |
| `finalize` | 整轮判定；`--close` 允许先自动关闭可关闭阶段 | `[--close] --run [--output]` |
| `gate` | 出结果闸门：未收尾返回退出码 5 与 blockers | `--run [--output]` |

`finalize` 退出码：`COMPLETE`/`PARTIAL` 为 0，`WAITING_USER`/`IN_PROGRESS` 为 4，便于脚本识别"还没查完"。

## 3. 阶段与任务状态

- 全局顺序：`official → recruitment → campus → bendibao_web → public_accounts`。用户明确只选部分渠道时按所选裁剪，不强行补回未选渠道。
- `campus`（高校就业网，渠道名 `campus_job`）是独立阶段：排在招聘平台之后、本地宝之前。入口与执行要点见[campus.md](campus.md)。
- **预算分渠道配额**：`budget.per_channel = {"official_career": 10, "51job": 5, ...}`。给了就按渠道各自计数、互不侵占；某渠道用完时该渠道回执被拒收（报错写明配额与已用次数），剩余任务在关闭阶段时转 `budget_exhausted`。没给 `per_channel` 则退回单池模式（`searches_per_city` 全渠道共享）。`budget_used` 同时记三本账：`searches`（城市总）、`by_stage`（阶段）、`by_channel`（渠道）。
- **渠道必须由用户显式选择**：`start-run` 不接受"按配置默默决定"。缺 `--channel/--all-channels` 直接报错并列出可选渠道；选了计划里没有的渠道也报错。渠道清单以 `workflow.CHANNEL_INFO` 为准（官网、前程无忧、智联、BOSS、猎聘、领英、高校就业网、本地宝网站、本地宝公众号）。
- 只覆盖部分阶段时，运行会记录 `scope.coverage=partial_scope`：用户确认过才可能判 `COMPLETE`，否则最高 `PARTIAL` 并写明未纳入哪些阶段。
- 渠道映射：`official_career/public_recruitment → official`；`zhaopin/boss/liepin/51job/linkedin → recruitment`；`campus_job → campus`；`bendibao_web → bendibao_web`；`bendibao_wechat → public_accounts`；`manual_import` 不进入任务簿。
- 任务状态：`pending`、`executed`、`failed`（可重试）、`blocked`、`skipped_by_user`、`budget_exhausted`。终止态只有后四个中的 `executed/blocked/skipped_by_user/budget_exhausted`。
- 单任务最多尝试 2 次，之后转 `blocked`（原因附"重试耗尽"），阶段其余任务继续。
- `not_run`/缺失记录**不能**作为关闭阶段的依据：阶段关闭要求所有任务进入终止态。
- 阶段关闭还有两道额外闸门（`close-stage` 会直接报错，不只是提示）：
  1. **没搜到底不给关**：存在 `exhausted=false` 且该城市**预算仍有剩余**的任务 → 拒绝关闭，`next-action` 返回 `continue_channel`。要继续就 `register_derived` 派生下一页/下一轮任务；确实到底就重新提交该任务回执并带 `exhausted=true`。
  2. **线索没核实不给关**：`direct/research` 任务产出的企业数 > 已登记核实数 + 不可核实数 → 拒绝关闭，`next-action` 返回 `register_derived`。
- 官网阶段预算账独立核算（`budget_used.by_stage`）：核实任务一直做到官网预算耗尽，耗尽后剩余核实任务转 `budget_exhausted`，阶段 `closed_with_gaps`。
- 带官网链接的企业**不能豁免**：`company_waivers` 里出现 `identified` 中有 url 的企业会被拒收。
- 预算用尽后仍未声明到底的任务，关闭阶段时自动转 `budget_exhausted`（记原因"搜索预算已用尽，检索未到底"），阶段状态为 `closed_with_gaps`，整轮最高 `PARTIAL`。
- 升级前跑的任务、旧协议回执没有 `exhausted` 字段：允许读取，但标记为未验证，且把 `COMPLETE` 降级为 `PARTIAL`（`N 条检索未声明是否搜到底`）。

## 4. 回执

`record-action` 的 `action` 取值：

| action | 必填 | 说明 |
|---|---|---|
| `search` | `task_id`、`status`、`executed_at`、`search_tool`、`candidate_count`、**`exhausted`**、**`identified`**（官网发现类必填） | 执行查询或读取入口。`status ∈ success/failed/restricted/budget_exhausted`。`exhausted` 表示该检索是否已到底（无更多页/无更多结果=true）；`route ∈ direct/research/browse_list/channel_search` 时**必填，缺了直接报错**。同一回执重复提交幂等，不重复扣预算 |
| `register_derived` | `task_id`、`items`、`kind`（company/page） | 把发现到的企业线索或后续页登记成任务。`kind=company` 生成同阶段的 `official_verify` 核实任务；`kind=page` 生成下一页任务并让父任务交出"到底"义务 |
| `auth_open` | `channel`、`browser_context_id`、`tab_id` | 打开登录入口，转 `waiting_user` |
| `auth_probe` | `channel`、`browser_context_id`、`tab_id`、`snapshot_path`、`url` | 提交登录核验；核心自己读快照、算哈希、匹配标记 |
| `skip` | `channel` 或 `task_id`、`user_confirmed=true`、`reason` | 用户明确跳过的渠道/任务 |
| `blocked` | `channel`、`evidence_ref`、`reason` | 受阻，必须带能力检测或访问失败证据 |
| `scope_adjust` | `user_confirmed=true`、`detail` | 用户明确调整范围 |

**`identified` 是核算分母。**官网发现类检索（`research`，或 `candidate_count>0` 的 `direct`）必须附 `identified`：这次检索里识别到的**企业全名单**，哪怕不打算核实也要列出来。程序用 `max(candidate_count, len(identified))` 作为分母核对，`candidate_count` 少报不能减少要处置的企业数。实测教训：只报"我挑了 8 家"、把另外 30 多家写成一句"交由平台渠道覆盖"，会让整轮看起来像跑完了。

**官网发现的企业线索必须逐家登记核实任务。**`route ∈ direct/research` 的 `search` 回执，只要 `candidate_count > registered(derived) + waived(company_waivers)`，核心直接拒收：要么在 `derived` 里列出企业（自动生成同阶段 `official_verify` 任务），要么在 `company_waivers` 里逐条给不可核实的理由。没有第三条路可以绕过。

回执**只能**提交结果字段；`task_id/query/target_city/role_keyword/channel/route/stage/company` 是计划字段，被回执改写会直接报错。合并时计划字段恒为准，回执多余字段被忽略。

## 5. 登录核验

- 只对招聘平台生效（`platform_auth_policy=required`）。`public_only` 是用户显式选择的匿名模式，控制器不再要求登录。
- 核验由核心做，**不接受**"我登录了"这种自述，也不接受"薪资显示数字"这种内容信号：
  1. 快照必须落在 `data/runs/<run_id>/` 内；
  2. 核心读取快照、计算 sha256（提交的 `dom_hash` 只用于比对，以核心计算为准）；
  3. 平台适配器定义的账号专属 UI 标记必须在快照中出现（如 51job 的"我的51job/退出登录"）；
  4. 出现登录表单/验证码遮罩标记则直接判失败；
  5. `browser_context_id`、`tab_id` 必须与 `auth_open` 记录一致；
  6. 有效期 120 分钟，过期或会话变更需重新核验。
- 授权范围是**用户可见浏览器会话**（`session_scope=browser_visible_dom`），不能据此认为无状态 Python HTTP 请求也已登录。
- 平台适配器（登录入口、标记、平台域名）在 `platform_auth.py` 中集中定义，新增平台必须同时补齐标记与域名，不能只加 URL。

## 6. 完成度

`finalize` 输出 `verdict` 与逐阶段、逐渠道明细：

| verdict | 判定条件（按优先级） |
|---|---|
| `WAITING_USER` | 仍有 `pending` 任务，或仍有平台未完成登录核验 |
| `IN_PROGRESS` | 仍有非 `pending` 的未处置任务（如失败待重试） |
| `PARTIAL` | 有 `blocked`/`skipped_by_user`/`budget_exhausted` 缺口；或本轮零执行；或启用渠道完全没有任务记录；或阶段未关闭 |
| `COMPLETE` | 声明范围内所有任务终止、所有渠道有处置、**所有检索都声明了 `exhausted`、官网线索全部核实或书面豁免**、无缺口 |

补充规则：

- `channels[*].not_exhausted` 统计该渠道未声明搜到底的任务数，非零时不会判 `COMPLETE`。
- 配置里启用但本轮没有任何任务的渠道，会以 `not_covered` 出现在 `channels` 中，并把 `COMPLETE` 降级为 `PARTIAL`（渠道级报告 `url=None` 也必须参与判定）。
- 浏览器采集（`process-browser`）只是官网阶段的一种读取方式：单企业覆盖完成 ≠ 官网阶段完成 ≠ 整轮完成。
- 阶段性导出允许随时进行，但 `discovery.completion.partial_export=true` 时，Excel 的"运行状态与完成度"表和命令行 `verdict` 都必须显式呈现未完成渠道与下一动作。

## 7. 兼容

- 没有 `stage` 字段的旧计划按 `channel` 推导阶段；旧回执不含阶段信息仍可用。
- 旧结果（无 `completion`）可继续 `export`，导出表不加完成度区块，十列格式不变。
- 旧任务接续新版流程时，用 `start-run` 建立新的运行上下文，历史证据时间与已发生消耗保留在旧文件里，不改写成新时间；旧记录不会被迁成"已通过新版检查"。

## 8. 边界（必须如实说明）

- 控制器只能约束**经过这些命令**的动作。宿主若绕过控制器直接写表、直接调用 `evaluate`/`export_bundle`，控制器无法拦截；此类产物不带 `run_id`/`completion`，应视为"未经控制器的结果"。
- 宿主未把搜索工具、浏览器动作接入控制器时，`record-action` 里提交的仍是宿主执行结果的声明。核心能做的是：拒绝覆盖计划字段、校验快照与标记、拒绝缺证据的 `blocked`、拒绝无执行时间的 `success`，但无法证明宿主工具真的被调用过。哈希只证明材料一致，不证明动作发生。
- 真实平台登录需要用户本人完成；受控测试只证明控制逻辑，不代表真实网站登录已验收。
