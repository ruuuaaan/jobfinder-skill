# 运行控制器协议（workflow-1.2）

适用版本：Skill 0.6.0 起。实现见 `scripts/runtime/jobfinder/workflow.py`、`channels.py`、`budgets.py`、
`receipts.py`、`platform_auth.py`、`access_control.py`。本协议只约束**经过本项目入口**的动作，边界见文末。

与 1.1 的差别（升级必读）：

| 项目 | 1.1 | 1.2 |
|---|---|---|
| 完成度粒度 | 渠道级（任一任务 `exhausted=true` 即视为该渠道到底） | `channel+city+query_scope+filter+chain` 五段范围 |
| 预算 | 派发按渠道配额、收尾按阶段账两套 | 一套 `budgets.cap()`，`per_city` 参与冻结 |
| 回执 | 字段清单三份、缺字段自动标 legacy | `receipt-2` 一份清单、缺必填直接报错 |
| 分批 | 同一 `plan_id` 导出过就拒绝后续 | 按 `run_id+task_id+attempt_id/receipt_id` 去重 |
| 登录 | 页面文本出现账号词即可 | 必须有真实打开事件 + 账号区域结构证据 |
| 浏览器 | 宿主内嵌预览可用 | 默认要求用户本机独立窗口；不可用时 `capability_gap` |

## 1. 运行状态

每次新检索创建一个 `run_id`，落盘在 `<workspace>/data/runs/<run_id>/`：

| 文件 | 内容 |
|---|---|
| `state.json` | schema、协议版本、回执协议、计划版本、配置指纹、范围、当前阶段、任务、预算、登录记录、范围调整、派生关系、回执台账 |
| `events.jsonl` | 逐条动作事件（`search` / `auth_open` / `auth_probe` / `auth_invalidated` / `skip` / `blocked` / `close_stage` / `scope_adjust`） |
| `completion.json` | `finalize` 的整轮判定 |
| `auth/*.json` | 登录核验快照（只能放在本目录下） |

`state.json` 关键字段：

```json
{
  "schema_version": "workflow-run-2",
  "protocol_version": "workflow-1.2",
  "receipt_protocol": "receipt-2",
  "run_id": "run-xxxx", "plan_id": "...", "config_fingerprint": "...",
  "scope": {"cities": ["北京"], "roles": ["财务BP"], "channels": ["official_career","zhaopin","linkedin"],
            "stage_order": ["official","recruitment"],
            "platform_auth_policy": "required",
            "browser_surface": "external",
            "user_scope_confirmed": true, "narrowed_by": "user",
            "budget": {"searches_per_city": 20,
                       "per_city": {"北京": 20}, "per_channel": {"official_career": 10, "zhaopin": 5, "linkedin": 5},
                       "per_city_channel": {}, "list_pages_per_city": null,
                       "detail_reads_per_city": null, "runtime_minutes": null}},
  "tasks": [{"task_id": "北京-3", "channel": "zhaopin", "stage": "recruitment",
             "route": "channel_search", "status": "pending", "attempts": 0,
             "query_scope": "财务BP", "filter_fingerprint": "9f1c…",
             "pagination_chain_id": "北京-3", "exhausted": null,
             "receipt_ids": [], "attempt_ids": []}],
  "auth": {"zhaopin": {"run_id": "run-xxxx", "channel": "zhaopin", "status": "verified",
                       "backend_session_id": "sess-1", "tab_id": "tab-1",
                       "surface": "external_browser", "checked_at": "...",
                       "opened_event": {"backend_session_id": "sess-1", "tab_id": "tab-1",
                                        "surface": "external_browser", "opened_at": "..."}}},
  "budget_used": {"searches": {"北京": 3},
                  "by_stage": {"recruitment": {"北京": 3}},
                  "by_channel": {"zhaopin": {"北京": 3}},
                  "by_kind": {"list_page": {"zhaopin": {"北京": 1}},
                              "detail_read": {"official_career": {"北京": 1}}},
                  "http_requests": {"北京": 12}},
  "platform_state": {"zhaopin": {"state": "cooling", "until": "...", "reason": "HTTP 429"}},
  "browser": {"status": "available", "session_id": "sess-1", "requires_local_connector": false},
  "receipt_ledger": {"run-xxxx|北京-3|att-1|rcp-1": {"at": "..."}}
}
```

## 2. 命令

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `channels` | 渠道菜单（阶段、能力差异、`access_mode`、默认配额） | `[--config] [--output]` |
| `start-run` | 冻结本轮范围、创建运行 | `--config --plan (--channel … 或 --all-channels) [--user-confirmed] [--public-only] [--browser-surface external\|embedded] [--run-id]` |
| `browser-status` | 本机浏览器能力（文件/进程/端口实测） | `[--output]` |
| `browser-stop` | 关闭专用浏览器进程 | `[--output]` |
| `auth-tabs` | 列出本阶段仍缺登录的平台 | `--run` |
| `auth-open` | 在独立窗口打开登录页，写打开事件 | `--run [--channel …]` |
| `auth-probe` | 读当前标签页快照并核验登录 | `--run --channel` |
| `next-action` | 返回当前唯一合法的下一动作 | `--run` |
| `record-action` | 提交执行/登录/受阻/跳过回执 | `--run --input <JSON>` |
| `close-stage` | 阶段内任务都有处置后关闭 | `[--stage] --run` |
| `finalize` | 整轮判定；`--close` 先自动关闭可关闭阶段 | `[--close] --run [--output]` |
| `gate` | 出结果闸门：未收尾返回退出码 5 | `--run [--output]` |
| `linkedin-profile` | 领英访问模式与能力 | `[--plan] [--output]` |
| `linkedin-import` | 领英人工材料入库导出 | `--config --input --output [--allow-example]` |
| `linkedin-search` | 领英受控浏览器小样本检索（1 搜索页 + ≤5 详情） | `--run --config --output --keyword … --location --user-confirmed [--max-details]` |

退出码：`finalize` 的 `COMPLETE`/`PARTIAL` 为 0，`WAITING_USER`/`IN_PROGRESS` 为 4；
`browser-status` 不可用为 6；`auth-probe` 未通过为 6；`gate` 未放行为 5；
`linkedin-search` 有岗位为 0、读到页面但无岗位为 5、需要用户处理（未登录/验证码）为 6。

## 3. 完成度按独立搜索范围计算

范围键 = `channel | city | query_scope | filter_fingerprint | pagination_chain_id`。

- `query_scope`：岗位词；列表页任务取 `role_keyword` 或合并后的 `covers_roles`；企业核实任务取 `verify:<企业>`。
- `filter_fingerprint`：路由、列表页 URL、覆盖岗位、计量类型的稳定哈希；同一范围翻页继承同一指纹。
- `pagination_chain_id`：列表页用 `browse_url`（p1…pn 同一条链），其它任务用自身 `task_id`；
  `register_derived(kind=page)` 的子任务继承父链。

规则：

1. 某范围任一任务 `exhausted=true` → 该范围到底；其它范围不受影响。
2. 范围内已执行但 `exhausted=false`、预算仍有剩余、且没有后续任务承接 → `close-stage` **失败**，
   `next-action` 返回 `continue_channel`。
3. 预算耗尽 → 范围记 `budget_limited`，任务转 `budget_exhausted`，原因写明"配额已用尽，覆盖缺口"，
   **不得**写成"网站没有更多结果"。
4. `COMPLETE` 只表示本轮声明范围完成，不代表站点全部岗位已查尽（`completion.reasons` 里明说）。

## 4. 预算

统一解析链：`per_city_channel[城市][渠道] → per_channel[渠道] → per_city[城市] → searches_per_city`。

- 一次真实执行 = 1 次 `search`（渠道账）+ 1 次计量类型（`list_page` / `detail_read`）。
- 渠道配额只约束 `search`；计量类型各有上限（`list_pages_per_city` / `detail_reads_per_city`），
  因此"一条查询"不能绕过详情读取预算。
- `by_stage`、`searches` 只用于报告，**不再作为第二道门**。
- 派发（`_next_task`）、回执（`record-action`）、收尾（`close_stage`）都调用
  `task_budget_remaining()`，三处结论一致。
- `start-run` 冻结 `budget.per_city`；恢复运行不重置消耗，重放不重复扣费。
- 细粒度上限从计划的 `budget_profile` 冻结，不写进配置模型（避免改变历史指纹）。

## 5. 回执（receipt-2）

`record-action` 的 `action`：

| action | 必填 | 说明 |
|---|---|---|
| `search` | `task_id`、`status`、`executed_at`、`search_tool`、`candidate_count`、**`exhausted`**、**`identified`**（官网发现类） | `route ∈ direct/research/browse_list/channel_search` 时 `exhausted` 必填，缺了直接报错 |
| `register_derived` | `task_id`、`items`、`kind`（company/page） | 企业线索或后续页登记；`kind=page` 继承分页链 |
| `auth_open` | `channel`、`browser_session_id`、`tab_id`、`surface` | 必须是真实浏览器打开事件；只有链接会被拒收 |
| `auth_probe` | `channel`、`browser_session_id`、`tab_id`、`snapshot_path`、`url` | 核心自己读快照、算哈希、核对账号区域 |
| `user_claimed_login` | `channel`（`reason` 可选） | 用户声明"我登录了"但结构证据不足：状态转 `reconfirm_required`，`next-action` 给出 `wait_user_reconfirm` 与 `reconfirm.steps`；不放宽 verified 门槛 |
| `skip` | `channel`/`task_id`、`user_confirmed=true`、`reason` | 用户明确跳过 |
| `blocked` | `channel`、`evidence_ref`、`reason` | 受阻必须带证据 |
| `scope_adjust` | `user_confirmed=true`、`detail` | 用户明确调整范围 |

版本识别：`receipt_version` / `protocol_version` / 运行状态里的 `receipt_protocol`。缺版本时按运行协议处理：
`receipt-1` 运行接受旧回执（缺 `exhausted` 标未验证），`receipt-2` 运行缺必填直接报错，
**不会**因为缺字段就降级成旧协议。

身份与幂等：

- `attempt_id` 标识"这一次执行"，`receipt_id` 标识"这一条回执"。
- 同 `receipt_id` 重放 → `duplicate:true`，不扣预算、不改证据时间；**预算耗尽后重放仍成功**。
- 同 `attempt_id` 新回执（补 `exhausted`/结束证据/派生）→ `revision:true`，更新字段、保留
  `revisions` 审计、不重复计次、不伪造新的 `executed_at`。

分批提交：`new-search --run` 按 `run_id+task_id+attempt_id/receipt_id` 判重。同一计划可以分多批，
第二批只提交此前未执行的任务；重复提交同一批会被明确拒绝，而不是整计划作废。

## 6. 登录核验

只对平台渠道生效（`platform_auth_policy=required`）。通过条件（全部满足）：

1. 存在 `open_auth_tab` 产生的打开事件，含 `backend_session_id`、`tab_id`、`surface=external_browser`；
2. 快照在本 run 目录内，核心自己读、自己算 sha256；
3. `browser_session_id`/`tab_id` 与打开事件一致，且当前浏览器进程仍持同一 `session_id`；
4. URL 属于该平台，且不含 `/login`、`/authwall`、`/checkpoint`、`/challenge`、`/uas/`、`/verify` 等路径；
5. 快照无密码输入框、无登录表单提交入口、无验证码组件；
6. 导航锚点里能找到该平台的**账号区域链接**，头部/导航有账号视图标志文案；
7. 页面不含登录/验证/受限文案。

失效条件：浏览器进程关闭或重建（`session_id` 变化）、标签页被关、退出或切换账号、超过 120 分钟。
失效后 `auth_status` 返回 `invalidated`，`next-action` 重新给出 `open_auth_tab`，任务保持 `pending`。

`public_only` 是用户显式选择的匿名模式，控制器不再要求登录；它不是默认值，助手不得自行切换。

## 7. 平台访问状态（跨 run）

`data/state/platforms.json` 保存每个平台的冷却/封禁：

- 429 / 频率限制 → `cooldown_until`（按 `retry_after` 或默认 300 秒）；
- 验证码、安全检查、明确拒绝、账号限制 → `blocked=true`，需用户显式重试才能解除；
- 成功执行 → 清除冷却标记。

重启、新建 run、换关键词或换城市都不清除。冷却期间 `next-action` 返回 `channel_cooling`，
其它可访问渠道照常推进；受阻渠道需记 `blocked`/`skip` 后才能关闭阶段，并以缺口计入 `PARTIAL`。

## 8. 完成度

| verdict | 判定条件（按优先级） |
|---|---|
| `WAITING_USER` | 仍有 `pending` 任务，或仍有平台未完成登录核验 |
| `IN_PROGRESS` | 仍有非 `pending` 的未处置任务（如失败待重试） |
| `PARTIAL` | 有 `blocked`/`skipped_by_user`/`budget_exhausted` 缺口；或本轮零执行；或启用渠道完全没任务；或阶段未关闭；或范围未经用户确认 |
| `COMPLETE` | 声明范围内所有搜索范围到底、所有任务终止、线索全部核实或书面豁免、无缺口、范围经用户确认 |

`completion` 同时输出：`scopes`（逐范围状态）、`channels`（含 `budget_cap`/`budget_used`/
`list_page_used`/`detail_read_used`/`not_exhausted`/`access_mode`/`platform_state`）、
`access`（预算面板、浏览器状态、平台状态、领英模式）、`narrowed_by`、`delivery_blockers`。

## 9. 兼容

- 无 `stage` 字段的旧计划按 `channel` 推导阶段。
- 旧 state（`workflow-run-1`）读取时补齐 `receipt_protocol=receipt-1`、任务范围字段、
  `budget.per_city`，**不改写** `created_at`、`executed_at`、`config_fingerprint`、
  `budget_used`；`upgraded_from` 留痕。
- 旧回执不含 `exhausted`：允许入库，标 `legacy_exhaustion`，`finalize` 降级为 `PARTIAL`。
- 旧结果（无 `completion`）仍可 `export`；没有完成度区块时十列格式不变。

## 10. 边界

- 控制器只能约束经过这些命令的动作。宿主绕过控制器直接写表、直接调用 `evaluate`/`export_bundle`，
  产物不带 `run_id`/`completion`，应视为"未经控制器的结果"。
- 浏览器动作是否真的发生，需要宿主通过 `auth-open` 回传真实句柄；核心能验证句柄一致性与页面证据，
  但无法远程证明某个工具真的被点击过。哈希只证明材料一致，不证明动作发生。
- 真实平台登录需要用户本人完成；受控测试只证明控制逻辑，不代表某站点登录已验收。
  实测记录见[验证记录](validation.md)。
