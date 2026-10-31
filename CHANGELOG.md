# 更新记录

## 0.5.3

- **有官网链接必须核实**：`identified` 支持 `{company,url}`；带 url 的企业不能进 `company_waivers`（拒收），只能逐家核实。
- **官网分阶段预算**：`budget_used.by_stage` 单独计账，核实一直做到官网预算耗尽；耗尽后剩余核实任务转 `budget_exhausted`，阶段 `closed_with_gaps`。
- **出结果闸门**：`finalize` 增加 `delivery_allowed` / `delivery_blockers`；新增 `gate` 命令（未通过退出码 5）。阶段未收尾（含本地宝、公众号）或渠道未搜到底时不许交付正式结果。
- 新增 5 个用例，共 64 个用例。

## 0.5.2

- 官网发现类回执新增必填 `identified`（本轮识别到的企业全名单）；分母改为 `max(candidate_count, len(identified))`。
  背景：实跑时只声明"我挑了 8 家核实"，实际检索点到 40 家，其余 32 家被一句"交由平台渠道覆盖"带过，整轮仍算通过。现在少报候选数不再能缩小分母。
- 新增 2 个用例（缺 `identified` 拒收、识别清单大于登记数时拒收），共 59 个用例。

## 0.5.1

执行协议升到 `workflow-1.1`：把"渠道要跑完"和"线索要逐家核实"从提示词变成拒绝入库的条件。

真实跑一轮上海财务BP时暴露两个漏洞：官网阶段报"发现 3 家企业"却没有任何核实任务，渠道只读了第 1 页就当作跑完，整轮仍判 COMPLETE。现在补掉：

- **`exhausted` 必填**：`route ∈ direct/research/browse_list/channel_search` 的 `search` 回执必须声明该检索是否已到底，缺了直接拒收（报错文案给出怎么继续）。
- **没搜到底不许关阶段**：`exhausted=false` 且该城市预算仍有剩余时，`close-stage` 报错，`next-action` 只返回 `continue_channel`，附带 `budget_remaining`。要继续用新动作 `register_derived kind=page` 派生下一页任务，父任务把"到底"义务交给新页（`continued=true`），避免父任务永远挂着缺口。
- **线索必须逐家核实**：`direct/research` 任务 `candidate_count > 已登记核实 + 书面豁免` 时拒收回执；`register_derived kind=company` 生成同阶段的 `official_verify` 任务；确实无法核实的要写 `company_waivers` 并给理由。官网阶段不核实完不许关。
- **预算用尽才算收工**：关闭阶段时，未声明到底且预算耗尽的任务自动转 `budget_exhausted`，阶段 `closed_with_gaps`，整轮最高 `PARTIAL`，不会再被算成完整搜索。
- **最终判定**：`channels[*].not_exhausted` 非零、或存在未核实线索、或存在升级前没标 `exhausted` 的检索，`COMPLETE` 一律降为 `PARTIAL` 并写明原因。
- 兼容：旧协议回执（无 `exhausted`）仍可入库，但标 `legacy_exhaustion=true` 并在判定里降级，不迁移成"已通过新版检查"。
- 测试：新增 6 个用例（缺 `exhausted` 拒收、未到底且预算未尽不许关阶段、派生页接管义务、预算耗尽记缺口、线索未登记拒收、派生核实任务卡住官网阶段），共 56 个用例全绿。

## 0.5.0

运行控制器（协议 `workflow-1`）：把"渠道顺序 + 登录前置 + 完成判定"从提示词搬进程序。

- 新增 `jobfinder/workflow.py`：`start-run / next-action / record-action / close-stage / finalize` 五个命令，运行状态落盘到 `data/runs/<run_id>/`。全局阶段顺序固定为 `official → recruitment → bendibao_web → public_accounts`，同一阶段内可按预算轮转城市/平台/任务，**跨阶段抢跑直接被拒**并返回当前允许的动作。用户明确只选部分渠道时按所选裁剪，不补回未选渠道。
- 新增 `jobfinder/platform_auth.py`：招聘平台必须先完成**当前会话登录核验**才能搜索。核验由核心读取页面快照、自己算哈希、自己匹配平台账号专属 UI 标记，并检查登录表单/验证码遮罩与 `browser_context_id`/`tab_id` 是否与开页记录一致；有效期 120 分钟。**"薪资显示数字"不再作为登录证据**（属于内容完整性）。授权范围仅限 `browser_visible_dom`，不能顺带宣称无状态 HTTP 已登录。`public_only` 是用户显式选择的匿名模式，助手不得自行切换。
- 任务状态区分 `pending / executed / failed / blocked / skipped_by_user / budget_exhausted`：单次失败只影响当前任务，最多 2 次尝试后转 `blocked`；`not_run` 或缺失记录不能作为关闭阶段依据；`skipped_by_user` 必须带用户确认，`blocked` 必须带能力检测或访问失败证据。
- 统一 finalizer：`new-search`、`process-browser`、`resume-traversal`、后续计划都写入/复用同一完成判定，输出 `COMPLETE / PARTIAL / WAITING_USER / IN_PROGRESS`。阶段性导出允许，但 Excel 的"运行状态与完成度"表与命令行 `verdict` 必须标注未完成渠道和下一动作；`finalize` 在未完成时返回退出码 4。
- 修复 0.4.0 的已知缺陷：
  - `build_plan` 无条件生成官网任务 → 现在只在启用 `official_career` 时生成官网 `direct/research/enterprise` 任务。
  - `browse_rows()` 对未适配城市返回空，但回退判断只看"岗位词是否命中类目"，导致该渠道彻底缺失 → 新增 `browse_covered_pairs()`，只有**实际生成了列表页任务**的「城市＋渠道＋岗位词」才取消站内搜索回退（北京等未适配城市现在会正常回退）。
  - `validate_receipt()` 合并时用 `task | receipt`，回执可以把 `channel`/`route`/`stage` 覆盖掉计划值 → 现在计划字段不可覆盖（直接报错），合并以计划为准、只叠加允许的结果字段。
  - 完成度检查中 `reports` 被 `if r.get("url")` 过滤，`url=None` 的渠道级记录不参与判定 → 现在渠道级报告参与判定；启用但完全没有任务记录的渠道以 `not_covered` 出现并把 `COMPLETE` 降级为 `PARTIAL`。
  - 空回执/空任务集因 `any([])` 语义被算成完成 → 现在零执行、零任务一律不判 `COMPLETE`。
  - `process-browser` 独立导出并自写 `status=complete` → 现在走统一 finalizer，浏览器采集不再能代表整轮完成。
  - CLI 只打印 `discovery.status` → 现在同时输出 `verdict`、`run_id` 与未完成任务，启动时打印 Skill 版本 / 协议版本 / 加载路径。
- 回执与预算：重复回执幂等（同哈希不重复扣费、不改证据时间）；阶段切换不清零额度；`skip/blocked/not_run` 不扣次数。
- 兼容：无 `stage` 字段的旧计划按 `channel` 推导阶段；旧结果仍可 `export`（十列格式不变，无完成度区块）；旧任务接续时用 `start-run` 建立新运行上下文，历史证据时间与已消耗预算保留。
- 文档：新增 `references/workflow.md`；重写 SKILL.md 的渠道顺序与登录章节为唯一主路径；统一 `setup.md`、`host-compatibility.md`、`budget.md` 中相互冲突的登录与顺序规则；明确标注"程序约束只覆盖经过控制器的动作"的边界。
- 测试：新增 `tests/`（39 个用例，纯合成数据与受控 DOM），覆盖阶段顺序、登录前置、跨平台/跨会话会话复用、受阻继续、四渠道完整性、回执不得覆盖计划、空回执、重试与预算、幂等恢复、旧计划兼容；`scripts/demo_workflow_trace.py` 提供"四阶段正常执行"和"某平台登录受阻后继续"两条受控轨迹。

## 0.4.0

- 计划改为**渠道 × 岗位词**生成任务：每个已选渠道都有自己的 `task_id` 与渠道额度，超出额度的任务标 `budget_exhausted` 保留不删；平台渠道不再借挂官网任务。
- 平台渠道新增**列表页直读**（`route=browse_list`）：一条任务一页，`query` 即该页 URL，一次可入库 20–40 条带薪资与职责原文；只收录实测通过的「城市 × 类目」组合，无实测则退回站内搜索，不猜 URL。
- 官网渠道改为三段式并新增两个子命令：**岗位查询词 → 企业名单 → 逐家官网**。`official-catalog` 从本渠道回执提取企业（按首次出现顺序，逐条留痕剔除原因），`official-plan` 逐家生成检索任务；渠道之间严格隔离，平台渠道发现的企业不进入官网名单。
- 新增**研究型检索**路由（`route=research`），委托宿主研究型检索工具产出「哪些企业在招」，解决官网渠道拿不到企业名单的问题；落平台域名的候选标 `lead_only`，须回官网核实。
- 明确**官网招聘页用浏览器读取**：静态抓取只能拿到前端渲染的空壳。补充三类页面形态与 URL 参数规律（自建 SPA、Moka、北森 zhiye），并规定官网普遍不披露薪资时薪资留空、由核心评估给出 `UNKNOWN` 落 REVIEW，不得按薪资未达排除，也不得用平台渠道薪资替官网岗位补值。
- 默认预算由每城 10 次提高为 **40 次**，非官网渠道默认各 **5 次**（可在 `search_budget.per_channel` 逐渠道覆盖）；官网额度恒等于「城市总预算 − 其它渠道合计」，单列官网额度直接报错；预算低于 `5 × 非官网渠道数` 时拒绝执行。
- 删除「城市 + 国资委 监管企业 名录 官方」类目录查询：目录页不是岗位来源，也判断不了企业是否在招。
- 新增 `references/setup.md` 与技能开篇两章：**首次安装配置（必做）**（A 运行环境 / B 联网搜索 / C 浏览器控制三级自检、已知坑、装完自检表）与**检索开始前：打开渠道页并登录（必做）**（四渠道登录入口与成功标志、薪资实证校验、不绕过验证码、不代填凭据）。
- 把「一页 = 1 次搜索」的口径统一到列表页直读、官网招聘页与研究型检索，三者记同一本预算账。

破坏性变更：计划任务新增 `channel` / `route` 字段；回执候选必须以 `task_ids` 命中本渠道任务，跨渠道挂载会被拒；官网优先策略下预算不足会直接报错；官网渠道不再接受目录页或名录类候选。旧配置若显式写了 `searches_per_city`，仍以显式值为准。

## 0.3.0-beta.1

- 分离 Skill 与本机 Agent 发布包，使用文件白名单构建，不携带个人配置、运行记录或 Git 历史。
- Skill 增加通用入口、独立工作目录、随包运行核心和地理数据；支持 agent_assisted 回执并兼容旧回执。
- Agent 使用共享业务规则，补齐普通 Python 虚拟环境安装、Windows 时区依赖和启动说明。
- 历史结果改为选择 JSON 导入，增加 JSON 导出，保留原条件、证据时间和结论。
- 对外表格统一为十列；软件版本与业务规则版本分别标明。增加 MIT 许可、发布校验及合成回归测试。
- 命令行与 Skill 入口显式使用 UTF-8 输出，支持英文 Windows 的重定向及管道调用。

Beta 表示跨网站采集仍需实际访问条件；不保证任意站点或任意宿主浏览器接口可自动适配。
