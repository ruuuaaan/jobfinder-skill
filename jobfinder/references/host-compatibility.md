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

## 已验证范围

自动化验证包括独立目录安装、普通 Python 子进程调用、通用查询回执、离线导出及旧回执兼容。没有安装并逐一操作所有第三方 agent；它们的技能发现、工具权限和浏览器接口需在首次使用时确认。Skill 不附带模型 API，也不提供独立联网后台；需要本机网页服务请选择 Agent 版。
