# 验证记录与验收矩阵（0.6.0）

本文件只记录**本版实际运行过**的结果。历史版本的数字不挪到这里冒充本版结果；
旧结果见各版本发布说明与工作区 `outputs/` 下的运行目录。

## 1. 怎么跑

```text
# 离线回归（不联网、不启动浏览器）
python -m unittest discover -s tests -v

# 真实本机浏览器验收（会启动本机 Chrome/Edge，默认跳过）
JOBFINDER_TEST_BROWSER=1 python -m unittest discover -s tests -p "test_browser_backend.py" -v
```

Windows PowerShell：

```powershell
$env:JOBFINDER_TEST_BROWSER="1"
python -m unittest discover -s tests -p "test_browser_backend.py" -v
```

## 2. 验收项与对应用例

| # | 验收要求 | 用例 | 类型 |
|---|---|---|---|
| 1 | 无浏览器打开事件时，仅凭含登录文字的页面快照不能通过 | `test_browser_auth.OpenEventTests.test_snapshot_with_login_words_without_open_event_is_rejected`、`test_open_action_without_browser_handle_is_rejected` | 离线 |
| 2 | 英文安全验证页即使含 My Network 也不判 verified | `test_browser_auth.SecurityPageTests.test_english_checkpoint_with_my_network_is_not_verified`、`test_marker_anywhere_on_page_is_not_enough` | 离线 |
| 3 | 会话关闭/失效/重建/换账号后，下一操作要求重新验证 | `test_browser_auth.SessionBindingTests`（3 例） | 离线 |
| 4 | 要求外部浏览器但不可连接时，不回退内嵌预览 | `test_browser_auth.NoEmbeddedFallbackTests.test_capability_gap_instead_of_embedded_preview`、`test_capability_report_ignores_host_claim` | 离线 |
| 5 | 同平台不同城市/岗位词/分页链的完成度互不覆盖 | `test_completion_scope.ScopeGranularityTests`（4 例） | 离线 |
| 6 | per_city 与 per_channel 在计划/执行/恢复/收尾中一致 | `test_budget_ledger.BudgetLedgerTests`（5 例） | 离线 |
| 7 | 官网配额耗尽且仍有派生核实任务时，能记缺口并收尾 | `test_completion_scope.BudgetExhaustionTests.test_official_quota_exhausted_with_pending_verifications_can_close` | 离线 |
| 8 | 新版回执字段经所有入口保持完整 | `test_receipts.ReceiptFieldPreservationTests`（4 例） | 离线 |
| 9 | 同一 plan+run 分两批提交不同任务可累计 | `test_receipts.BatchSubmissionTests`（3 例） | 离线 |
| 10 | 回执重放不重复扣预算；证据修订合法更新 | `test_receipts.IdempotencyAndRevisionTests`（3 例） | 离线 |
| 11 | 未确认的渠道缩小不能记为用户已确认 | `test_scope_and_cooldown.UserScopeConfirmationTests`（3 例） | 离线 |
| 12 | 风控冷却跨 run 保留，其它渠道按规则推进 | `test_scope_and_cooldown.PlatformCooldownTests`（4 例） | 离线 |
| 13 | 领英在菜单/全选/规划/来源/导出中一致 | `test_linkedin_quality.LinkedInChannelTests`（6 例） | 离线 |
| 14 | 英文学历优先项、等价经验、中英同义岗位不被硬排除 | `test_linkedin_quality.EnglishMatchingTests`（6 例） | 离线 |
| 15 | 阶段性主表能看出未完成；MATCH 的待确认说明不丢失 | `test_linkedin_quality.StagedExportTests.test_partial_summary_is_labelled_and_keeps_match_pending` | 离线 |
| 16 | 不同查询条件的报告展示各自真实条件 | `test_linkedin_quality.StagedExportTests.test_report_conditions_come_from_config` | 离线 |
| 17 | 领英浏览器辅助只读小样本：1 搜索页 + ≤5 详情，不翻页、不投递 | `test_linkedin_browser.BrowserSearchFlowTests.test_end_to_end_sample_reads_two_details`、`test_detail_limit_is_capped` | 离线（假浏览器） |
| 18 | 未登录/登录墙不能记成"零岗位" | `test_linkedin_browser.ParserTests.test_guest_homepage_is_login_required_not_zero_jobs`、`BrowserSearchFlowTests.test_login_wall_reports_login_required_and_invalidates` | 离线 + 真实站 |
| 19 | 预算不足先停、详情只记 `detail_read` | `test_budget_shortage_stops_details_and_records_gap`、`test_end_to_end_sample_reads_two_details` | 离线 |
| 20 | 验证码/安全检查停止并置 blocked（跨 run） | `test_checkpoint_stops_and_sets_cooldown` | 离线 |

补充回归：

- 历史兼容：`test_legacy_compat.py`（旧 state 升级、旧计划、旧回执、指纹不变、渠道覆盖降级）。
- 访问控制：`test_access_recovery.py`（平台锁、冷却、缓存键与复用、分动作计量）。
- 浏览器后端参数：`test_browser_backend.BackendUnitTests`（配置目录必须绝对路径、端口稳定、无浏览器时的能力缺口）。

## 3. 本版实际运行结果（2026-11-02，Windows 11，Python 3.13.14）

```text
$ python -m unittest discover -s tests
Ran 103 tests in 2.407s
OK (skipped=1)

$ JOBFINDER_TEST_BROWSER=1 python -m unittest discover -s tests -p "test_browser_backend.py"
Ran 4 tests in 6.27s
OK
```

`skipped=1` 是真实浏览器冒烟用例，需要显式打开环境变量才会启动本机浏览器。

## 4. 真实本机验收（做过的部分）

| 项目 | 做法 | 结果 |
|---|---|---|
| 浏览器探测 | `browser-status` | 检出 Chrome 156 与 Edge，`status=available_not_running`，`requires_local_connector=false` |
| 独立进程 + 专用配置目录 | `BrowserService.ensure()` | 启动成功，`service.json` 记录 pid/端口/`session_id`；同目录第二次 `ensure()` 复用同一 `session_id` |
| 真实标签页与 DOM | 打开本地 HTML 后 `snapshot()` | 取到 `title=JF`、锚点 `/mynetwork/`、`ready_state=complete` |
| 打开登录页 | `auth-open --run … --channel zhaopin` | 真实打开 `https://passport.zhaopin.com/login`，返回 `tab_id` 与 `browser_session_id` |
| 未登录核验 | `auth-probe --run … --channel zhaopin` | `status=failed`，原因为"仍停留在登录页（URL 命中 /login）""页面显示登录/验证内容""导航中找不到账号区域链接"；`next=open_auth_tab` |
| 领英人工材料 | `linkedin-import` | 1 条入库：`source_channel=linkedin`、`source_type=platform`、`apply_type=easy_apply`、薪资留空、学历"preferred"判 UNKNOWN、整体 REVIEW；导出阶段性十列 |
| 领英浏览器检索（未登录） | `linkedin-search --user-confirmed` | 真实访问 `/jobs/search` 被重定向到访客首页；返回 `login_required`、退出码 6，**没有**写成"0 个岗位" |
| 真实浏览器 + 结构化解析 | 本机 `file://` 页面（含 JSON-LD 与岗位锚点）经真实 Chrome 读取 | `json_ld` 节点 1 条、锚点 2 条、`h1` 正确；解析出 title/company/location/employmentType，生成 `linkedin/platform` 岗位证据 `browser_visible_dom` |

本轮浏览器以 `JOBFINDER_BROWSER_HEADLESS=1` 运行，避免在用户桌面弹出窗口；
可见窗口模式（默认）尚未在本机做过"用户真实登录"的完整链路。

## 5. 尚需用户配合的验收

| 项目 | 为什么需要用户 | 判定标准 |
|---|---|---|
| 可见窗口登录 | 必须由本人输入账号、可能还需验证码 | `auth-open` 后桌面出现独立窗口；登录完成后 `auth-probe` 返回 `verified` |
| 平台站内搜索 | 需要真实账号与平台许可 | 智联/前程无忧等按 `search_task` 取到列表并提交带 `exhausted` 的回执 |
| 领英站内搜索（登录后） | 必须有可用账号；平台禁止未授权批量抓取 | `auth-probe` 返回 `verified` 后运行 `linkedin-search`，应取到搜索页岗位卡并导出；仅限 1 页 + ≤5 详情 |
| 领英人工材料 | 用户需提供原文/链接 | `linkedin-import` 正常入库并匹配 |
| 跨会话持久化 | 需要关闭助手后重新打开浏览器 | 第二轮 `browser-status` 仍是同一 `session_id`，且无需重新登录 |

## 6. 声明

- 上述离线用例只证明**控制逻辑**，不证明任何真实站点已登录成功；
- 真实站点只用小样本只读检查，未提交申请、未联系招聘者；
- 没有做到的项目一律标"未实测"，不以 mock 通过替代实站通过。
