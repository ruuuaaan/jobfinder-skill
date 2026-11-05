# 变更与发布清单

## 0.6.2（2026-11-05）

补上"用户明明已登录、核验却判没登录"这条断链：平台渠道新增**宿主浏览器通道**，
并给出用户声明已登录后的重新确认流程，同时修掉 SPA 站点判不出登录的根因。

> 这批改动原先以本地补丁 `local-2026-11-02` 的形式直接改在安装目录，本次并入仓库正式发布。
> 安装目录里的 `local-patch/` 备份已无保留必要，升级或重装覆盖后不必再重新应用。

### 新增：宿主机浏览器通道（平台渠道的新默认路径）

- `platform_auth.HOST_BROWSER` / `verify_host_probe()` / `build_host_snapshot()` / `parse_host_dom()`：
  用宿主浏览器工具（WorkBuddy 内置或扩展连的用户浏览器）打开平台、用户自己登录，
  再以"同一 `browser_id` + 同一 `tab_id` + 快照 10 分钟内 + URL 非登录页 + DOM 无登录控件
  + DOM 有账号入口"六项成立判定 `verified`，记录标 `backend=host_browser`。
- CLI `host-open` / `host-probe`：登记宿主浏览器的打开事件与可见 DOM 快照。
- `receipts.META_FIELDS` 增加 `backend`/`browser_id`/`host_tool`/`pid`/`profile_dir`/`login_url`
  （原先会被回执协议静默丢弃，导致宿主模式无法识别）。
- `binding_problems` 对 `backend=host_browser` 跳过本机 `BrowserService` 存活检查。

### 新增：用户声明已登录后的重新确认流程

- 动作 `user_claimed_login` → 状态 `reconfirm_required` → `next-action` 返回 `wait_user_reconfirm`；
  CLI `auth-claim`。声明只影响引导，**不放宽 verified 门槛**，声明后仍不能提交业务搜索。
- `platform_auth.reconfirm_steps()` + `NAVIGATE_HINTS`：按平台给出"点右上角头像进入个人中心"的具体步骤，
  连续 3 次不通过回落 `open_auth_tab` 并提示按 `blocked` 处理。
- `finalize` 的 `unverified_auth` 纳入 `reconfirm_required`，仍判 `WAITING_USER`。

### 修复：登录核验的假阴性

- `platform_auth.header_signal()`：把失败原因区分成 `header_not_captured`（抓不到头部信号，不代表未登录）、
  `header_text_empty`、`ok` 三种，提示文案据此分化，不再一律说"没有账号视图标志文案"。
- `browser_backend._SNAPSHOT_JS`：头部识别从语义标签扩到类名/ID 关键字 + **首屏顶部 160px 导航带**；
  `header_text` 为空时用顶部链接文案兜底。修掉 51job / BOSS / 猎聘 这类 SPA 判不出登录的问题。
- `browser_backend._SNAPSHOT_JS`：锚点上限 500 → 2000。原先前程无忧类目页的岗位详情链接被截断，
  表现为"能读到岗位但没有投递链接"。
- `platform_auth.REQUIRED_MARKERS["liepin"]` 增加 `你好[，,\s]`（猎聘头部就是「你好，X」）。

### 仓库卫生

- 单测随包放进 `jobfinder/tests/`（118 例），CI 增加 `python -m unittest discover -s jobfinder/tests`；
  仓库根目录 0.5.x 时代的控制器集成测试（73 例，断言旧 `next-action` 收敛行为）移除，历史见 Git 记录。
- `scripts/smoke_release.py` 适配阶段性导出文件名（`岗位汇总_阶段性_<run_id>.xlsx`）。
- `scripts/demo_workflow_trace.py` 按 workflow-1 编写，0.6 起渠道清单与回执协议已变，移出 CI 并加注说明。

### 测试

- 新增 `jobfinder/tests/test_auth_reconfirm.py`（8 例）、`jobfinder/tests/test_host_browser_auth.py`（7 例）；
  全量 118 例通过（原 103 例），命令 `python -m unittest discover -s jobfinder/tests`。

## 0.6.1（2026-11-02）

领英补一条**可用的检索路径**：在"没有平台许可的批量抓取"与"用户手动复制原文"之间，
增加受控浏览器小样本检索，而不是把"人工辅助"当作唯一答案。

### 新增

- `linkedin-search` 命令与 `service.run_linkedin_browser_search()`：用已核验的本机登录会话
  打开 1 个站内搜索页，读取可见岗位卡，再打开至多 5 条详情（默认 3），把原文变成
  `source_channel=linkedin` / `source_type=platform` 的岗位后走同一套 evaluate/export。
- 浏览器快照新增结构化信号：`headings`（h1–h3）与 `json_ld`（含 JobPosting），
  解析优先用 JSON-LD，缺失才退回可见文本；`BrowserService.navigate()` 复用同一标签页。
- 领英解析器：`extract_job_links()`、`parse_job_snapshot()`、`job_from_browser_snapshot()`，
  支持 Easy Apply / 外部入口识别，缺核心字段直接报错而不是编造。
- 访问模式 `browser_assisted`：`browser_control_enabled=true`、`automation_enabled=false`、
  `bulk_listing_crawl=false`，能力报告如实区分"浏览器控制"与"平台许可自动化"。
- `workflow.charge_access()` / `record_browser_search()`：不经任务回执的动作也进同一本预算账。

### 修复

- **登录墙被误判为零岗位**：真实测试发现未登录访问 `linkedin.com/jobs/search` 会被重定向到
  访客首页（标题"登录或注册"），旧检测只认 `/login` 路径，于是记成"0 条结果"。
  现在识别访客首页/登录标题/密码输入框，返回 `login_required`（退出码 6）并使登录态失效。
- 验证码/安全检查现在同时置 `blocked`（原先只写冷却，可能被自动重试）。
- 详情读取只记 `detail_read`，不重复占用渠道搜索配额（一次采样不再被算成两次搜索）。
- 预算不足时**先停下再开页**，不白白打开详情页，也不把缺口写成"没有岗位"。

### 约束（写进代码）

用户显式同意（`--user-confirmed`）才驱动浏览器；只读 1 个搜索页 + ≤5 条详情；
页面间隔 ≥4 秒且跨 run 记录；平台串行锁；不翻页、不批量、不调内部接口、不点投递；
遇验证码/登录墙立即停止交回用户。批量自动抓取仍需平台许可证据。

### 测试

`python -m unittest discover -s tests` → 103 个用例通过（1 个真实浏览器用例默认跳过）。
新增 `tests/test_linkedin_browser.py`（17 例）：链接提取、JSON-LD/文本解析、登录墙与验证码、
预算不足、详情上限、地点必须在目标城市、端到端导出与预算记账。

## 0.6.0（2026-11-02）

协议版本：`workflow-1.2`；回执协议：`receipt-2`（兼容 `receipt-1`）；运行状态 schema：`workflow-run-2`（旧记录读取时自动补齐字段，不改写历史条件/证据时间/指纹）。

### 新增

- **本机独立浏览器后端** `browser_backend.py`：探测系统 Chrome/Edge，用专用配置目录
  （`<workspace>/data/browser/profile`）启动真实独立进程，CDP 新建标签页、读取可见 DOM、
  写快照；`service.json` 记录 pid/端口/`session_id`，多次 CLI 调用复用同一进程；
  `DevToolsActivePort` 与端口占用检查避免并发冲突。
- **CLI**：`browser-status`、`browser-stop`、`auth-open`、`auth-probe`、`linkedin-import`、`linkedin-profile`。
- **渠道注册表** `channels.py`：菜单、配置、规划、阶段映射、登录管理、导出中文名共用一份名单；
  领英进入菜单与 `--all-channels`。
- **统一预算账本** `budgets.py`：`per_city_channel → per_channel → per_city → searches_per_city`
  一条解析链；搜索/列表页/详情分别计量；`provider` 侧 HTTP 与运行时长单列。
- **版本化回执** `receipts.py`：`receipt-2` 缺必填字段直接报错、不降级；`receipt_id`/`attempt_id`
  区分"重放"与"修订"。
- **访问控制** `access_control.py`：平台串行文件锁、跨 run 冷却/封禁状态、页面级缓存。
- **领英渠道** `linkedin.py`：`assisted_manual`（默认）/`licensed_api`（需许可证据）；
  用户材料导入与来源/事实规则。
- **结果质量** `quality.py`：Suitability / Evidence / Application 三态，与旧 MATCH/REVIEW/EXCLUDED 并存。

### 修复

1. 完成度按 `channel + city + query_scope + filter_fingerprint + pagination_chain_id` 计算；
   北京到底不再覆盖深圳，岗位词 A 到底不再覆盖岗位词 B，末页只关闭本分页链。
2. `start-run` 冻结预算补上 `budget.per_city`；派发、回执、恢复、收尾共用 `budgets.cap()`。
   官网核实不再用 `by_stage` 的第二套账判断。
3. 回执字段清单合并为一份，`exhausted/identified/derived/company_waivers` 不再在批量入口丢失。
4. `ensure_new_execution` 改为按 `run_id + task_id + attempt_id/receipt_id` 判重，支持同轮分批提交；
   不再因某个 `plan_id` 导出过就拒绝后续任务。
5. 回执重放先于配额检查：预算耗尽后重放仍幂等成功；同一 `attempt_id` 补 `exhausted`/证据
   记为修订，不重复计次、不改原执行时间。
6. `--user-confirmed` 不再被忽略；助手自行收窄记 `narrowed_by=agent`，最终最高 `PARTIAL`。
7. 登录核验要求真实打开事件 + 会话绑定 + 账号区域链接 + 头部标志；英文关卡（authwall/checkpoint/
   challenge/sign in）全部识别；快照缺结构信号不再能通过。
8. 能力报告来自文件系统/进程/端口探测，模型自述 `visible_browser=true` 不作数；
   不可用时返回结构化 `capability_gap`，不回退内嵌预览。
9. 报告/说明中写死的"四家企业""广州·财务BP·本科及以上·社会招聘""九列表格"改为按配置与结果生成。
10. 汇总表 `MATCH` 行不再隐藏未确认事项与投递入口状态；阶段性导出文件名与说明页标注运行 ID、
    覆盖范围与缺口。
11. 英文学历解析：`Master's degree preferred`→UNKNOWN、`Bachelor's degree or equivalent experience`
    →UNKNOWN、`Bachelor's degree required, Master's preferred`→BACHELOR。
12. 岗位标题匹配引入已确认中英同义词；英文标题未命中同义词时转 `UNKNOWN` 而不是硬排除。

### 已知限制

- 可见窗口的"用户真实登录"链路未在本机复测（需用户配合，见 `references/validation.md`）；
- 领英没有自动化抓取能力，只有人工辅助；`licensed_api` 需用户提供许可证据；
- 滚动虚拟列表、多层 iframe、复杂交互搜索未新增适配；
- 列表页直读仍只覆盖 `BROWSE_CATALOG` 收录的城市与类目，未收录的回退站内搜索。

### 升级注意

- 旧配置指纹不变（有回归用例守护）；新增预算字段放在计划的 `budget_profile`，不进配置模型。
- 旧运行记录读取时升级为 `workflow-run-2` 并标 `receipt_protocol=receipt-1`；历史 `exhausted`
  缺失仍按"未验证"处理，不会事后补成 `true`。
- 旧回执（无 `receipt_version`）只在 `receipt-1` 运行中被接受；新建运行默认 `receipt-2`。
