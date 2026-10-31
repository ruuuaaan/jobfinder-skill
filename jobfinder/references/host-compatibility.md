# 跨 Agent 安装与能力契约

本包遵循 [Agent Skills 目录规范](https://agentskills.io/specification)。不要求特定模型、订阅或 Codex；宿主必须能读取技能文件、调用终端、读写工作目录。实时发现还需要获准的搜索工具，动态页面需要浏览器工具。纯聊天窗口无法执行本技能。

## 安装和工作目录

将完整的 jobfinder 目录放入宿主说明的技能目录。不同 agent 的自动发现位置不同；不支持自动发现时，可显式要求它读取 SKILL.md 并按其流程执行。agents/openai.yaml 只是可选的界面元数据，其他宿主可忽略。

Python 3.11+。在技能目录以外创建虚拟环境，用同一个解释器安装技能根目录 requirements.txt：
```text
python -m venv <ENV>
<ENV_PYTHON> -m pip install -r "<SKILL_DIR>/requirements.txt"
<ENV_PYTHON> "<SKILL_DIR>/scripts/run.py" --workspace "<WORKSPACE>" init
<ENV_PYTHON> "<SKILL_DIR>/scripts/run.py" --workspace "<WORKSPACE>" offline --config configs/offline.synthetic.json --candidates tests/fixtures/candidates.synthetic.json --output outputs/test/first-run --allow-example
```
ENV_PYTHON 在 Windows 是 ENV/Scripts/python.exe，在 macOS/Linux 是 ENV/bin/python。所有占位符换成实际路径；init 不覆盖已有文件。看到 outputs/test/first-run/岗位汇总.xlsx 表示核心离线导出链路可用，不代表进行了网络搜索。

真实任务复制 configs/user.template.json 到工作目录的新配置；填写用户本轮实际条件后才把 user_input_confirmed 设为 true。不得直接用演示条件替用户搜索。真实结果放 outputs/real/<唯一目录>，合成结果放 outputs/test/<唯一目录>。核心只读写指定工作目录内的显式材料。

## 工具能力映射

| 核心步骤 | 宿主需要提供 | 能力缺失时 |
|---|---|---|
| 理解并展开岗位 | 模型及文件工具 | 报告不能执行 |
| 生成计划、校验、匹配、导出 | Python 终端 | 提示安装依赖，不手工冒充运行 |
| 执行查询 | 宿主正式搜索工具 | 可处理用户提供的链接，标 provided_links，不能称新搜索 |
| 读取公开静态页面 | CLI 的已审核 HTTP 访问策略或宿主网页工具 | 保留候选与原因 |
| 动态列表、iframe、登录页 | 宿主允许的浏览器工具 | 标 partial/needs_help，继续可访问渠道 |
| 运行可选 browser_collector.mjs | 可导入 JS 模块且 Browser/Tab 接口兼容 | 使用宿主原生步骤；不冒充适配器完成 |

先用一条搜索和一个可访问详情确认宿主能力。工具名、权限、会话入口以宿主实际文档为准，不照搬 Codex 工具调用，不私自启用另一套浏览器或读取登录凭据。

## 通用查询回执

先运行 plan。每次实际搜索对应计划的一项 task，原样保留 task_id、query、target_city、role_keyword。回执顶层：
```json
{
  "mode": "agent_assisted",
  "plan_id": "<从本次计划复制>",
  "config_fingerprint": "<从本次计划复制>",
  "search_mode": "not_exposed",
  "coverage": "<本轮实际覆盖及限制>",
  "search_log": [],
  "candidates": []
}
```
执行过的 search_log 每项还需 executed_at（带时区）、search_tool（实际宿主工具名）、status 和 candidate_count。状态只用 success/failed/restricted/not_run/budget_exhausted；没执行的查询绝不伪造时间或成功。失败和回退也消耗预算；日志、预算、候选数量由核心核对。

candidates 每项至少包含 query、url、title、discovered_at、source_channel；新检索还须有 task_ids（关联成功查询）、target_cities（与查询一致）。source_type、source_evidence、company、candidate_type 等据实填写。原文没读到时不能用搜索摘要补全。旧 codex_assisted 回执仍兼容，不修改旧指纹。

## 浏览器与手工材料

共用自动采集脚本需要 Browser.tabs.new/list/get 和 Tab.goto/playwright 等兼容接口，属于可选优化。任意宿主均可在其允许的工具中逐步读取实际页面并保存原文材料；独立 CLI 可通过 authorized_import 的本地 HTML/JSON 候选处理材料，标记 manual_import，不能伪装为 browser_visible_dom 或官方完整详情。字段原文和来源关系仍需保存。

没有适配器时不要手工编造 browser-run.json 的成功轨迹。可以保留平台卡片为 REVIEW；遇登录、验证码或拒绝按宿主规则交给用户处理，不绕过。

首次在本机启用浏览器通道的开关入口、扩展安装、自检方法和证据等级，见[首次安装配置清单](setup.md)。未完成配置时，控制器把该平台记为 `blocked`（附能力检测证据）并继续其它可访问渠道，不写成"零岗位"；登录态证据记 `browser_visible_dom`，不能冒充 Python HTTP 成功或官方完整详情。

## 宿主适配接口（登录与可见浏览器）

控制器不假设任何品牌的 API。宿主需要把下列能力映射到自己的工具上，并把结果作为 `record-action` 的输入交给核心：

| 接口 | 用途 | 缺失时的表现 |
|---|---|---|
| `detect_capabilities` | 报告是否有用户可见、可操作的浏览器、能否读 DOM、能否把控制权交给用户 | 该平台记 `blocked`，理由写"未提供可见浏览器/登录交接" |
| `open_visible_tab` | 打开平台登录入口或目标页，返回 `browser_context_id`、`tab_id` | 无法进入 `wait_user_auth`，只能 `blocked` |
| `read_dom` | 读取当前页面可见内容并存成本轮快照文件 | 登录核验无法进行（核心必须自己读快照） |
| `handoff_to_user` | 让用户在同一标签页完成登录、验证码、二次验证 | 记录 `waiting_user`，等待用户 |
| `inspect_session` | 登录后重新读取页面，产出核验用快照 | 无法 `auth_probe`，任务保持 `pending` |
| `resume` | 登录完成后回到原任务 | 该平台任务不推进，其他渠道继续 |

约束：

- 快照必须由宿主写成文件并落在 `data/runs/<run_id>/` 内；核心自己读文件、自己算哈希、自己匹配平台标记。宿主不得直接把 `logged_in=true` 传给核心。
- `browser_context_id`/`tab_id` 由宿主提供，核心只校验与 `auth_open` 记录一致；换了会话或标签页要重新核验。
- 不读取、不导出 Cookie 与账号凭据；不代填密码；遇验证码停下交用户处理。
- 没有该接口的宿主（例如纯终端 agent）仍可使用 `public_only` 匿名模式，或把平台记 `blocked` 后继续其他渠道——但**不能**把该平台算作已完成。

协议细节与状态机见[运行控制器协议](workflow.md)。

## 边界：控制器能管什么

经过 `start-run / next-action / record-action / close-stage / finalize` 的动作受程序约束：跨阶段抢跑会被拒、计划字段不可覆盖、无登录核验不能搜索、阶段未闭合不能推进、整轮未完成不能判 `COMPLETE`。

不经过这些入口的动作管不到：宿主若自行调用匹配/导出函数、自己写 Excel，产物里不会有 `run_id` 与完成度区块。这不是本技能的"已完成检索"，使用方应据此判断。搜索工具与浏览器动作是否真的发生，需要宿主把工具调用结果回传；核心只能做结构校验与证据校验，不能远程证明。

## 已验证范围

自动化验证包括独立目录安装、普通 Python 子进程调用、通用查询回执、离线导出、旧回执兼容，以及运行控制器的阶段顺序 / 登录前置 / 完成度判定（`tests/` 下的合成数据与受控 DOM）。没有安装并逐一操作所有第三方 agent；它们的技能发现、工具权限和浏览器接口需在首次使用时确认。**真实平台登录尚未在本仓库内验证**：受控测试只证明控制逻辑，不代表某站点登录已验收。Skill 不附带模型 API，也不提供独立联网后台；需要本机网页服务请选择 Agent 版。
