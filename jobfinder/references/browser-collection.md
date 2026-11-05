# 可见浏览器采集与登录交接

适用于官方招聘站只返回标题、JavaScript 空壳、iframe，或职位点击后进入新标签页的情况；平台渠道的登录交接也走这里。

## 0. 两条通道：宿主机优先，专用 profile 备用

| 通道 | 怎么用 | 什么时候用 |
|---|---|---|
| **宿主机浏览器（默认）** | 宿主自带的浏览器工具（WorkBuddy 内置浏览器，或扩展连的用户自己浏览器）打开平台 → 用户自己登录 → 用工具的输入/点击在搜索框检索 → 读可见 DOM → `host-open` + `host-probe` 登记核验 | 平台渠道一律优先。用户日常浏览器里本来就有登录态，也不用再维护一套专用 profile |
| 专用 profile（备用） | `auth-open` 启动 `<workspace>/data/browser/profile`，真实进程 + CDP，`service.json` 记录 pid/端口/`session_id`；`auth-probe` 读快照核验 | 宿主机没有可用浏览器工具、或需要与外网完全隔离的一次性环境 |

两条通道都要求：**登录由用户本人在那个窗口里完成**；核心只读快照并核验；
**登录页不得出现在 agent 的内嵌浏览器预览里**：`--browser-surface embedded` 必须由用户显式选择，
`capability_gap` 也不会自动降级成内嵌。

### 0.1 宿主机通道的核验链

宿主机通道没有本机进程可探测，绑定强度靠三件事同时成立：

1. **同一 `browser_id`**：换浏览器实例即失效；
2. **同一 `tab_id`**：换标签页即失效（用户在自己浏览器里新开标签页也算换）；
3. **快照 10 分钟内**：过时要求重新读取当前页面。

再加三条硬规则：URL 属于该平台且不在登录/风控路径；DOM 里没有密码框、验证码、`登录/注册` 之类控件；
DOM 里必须出现**登录后才有的账号入口**（我的简历 / 投递记录 / 个人中心 / 你好，X）。

记录里写 `backend=host_browser`，`verification_basis` 为
`host_open_event + host_tab_binding + url_path + account_region_host_dom + no_login_control`。
证据等级是 `browser_visible_dom`，不能记成 Python HTTP 成功，也不能当作官方完整详情。

**失败时的处理**：先区分"抓不到证据"和"确实没登录"。前者（DOM 结构不配合）用 `auth-claim` 记录用户声明，
按 `reconfirm.steps` 让用户点进账号页面再核验；后者直接提示用户去登录。不要反复弹登录页。

## 0.2 专用 profile 后端（备用通道）

| 能力 | 实现 | 说明 |
|---|---|---|
| 探测 | `browser-status` | 枚举已安装 Chrome/Edge、读取 `service.json`、探测调试端口；**不接受**模型自述 `available=true` |
| 启动 | `BrowserService.ensure()` | 优先复用同配置目录的进程；`DevToolsActivePort` 校正实际端口；端口占用时明确报错，不并发启动 |
| 打开标签 | `auth-open` | 返回真实 `tab_id` + `browser_session_id` + `surface=external_browser` |
| 读 DOM | `snapshot()` | 文本 + 锚点（href/text/in_header）+ 头部文本 + 密码框/登录表单/验证码组件计数 + `content_hash`；加载中有限等待，不把空壳当"没有内容" |
| 会话绑定 | `service.json` 的 `session_id` | 重建进程或换配置目录即变化，登录核验随之失效 |
| 关闭 | `browser-stop` | 关闭整个专用浏览器，不影响用户自己的浏览器窗口 |

环境变量：`JOBFINDER_BROWSER_PATH`（指定可执行文件）、`JOBFINDER_BROWSER_PROFILE`（换配置目录）、
`JOBFINDER_BROWSER_HEADLESS=1`（无图形会话/CI 用；正常使用不要开，否则用户看不到登录窗口）。

已知坑（都是实测踩到的）：

- `--user-data-dir` **必须是绝对路径**。Chrome 136 起禁止在默认用户目录开启远程调试；传相对路径会
  静默退回默认配置目录，表现为"进程启动成功但调试端口永远不就绪"。
- 顶层 `data:` URL 在部分版本被拦，验证后端时用本地 `file://` 页面或真实 https 页面。
- Windows 下用 `DETACHED_PROCESS` 启动，让窗口在 CLI 退出后继续存在；进程被外部杀掉时，
  `session_id` 变化会让登录态失效，这是预期行为。

## 0.3 采集回执（browser-run）

适用于批量读取官网/平台页面：使用宿主当前浏览器工具读取实际可见页面。独立 Python CLI 只验证和处理回执；
随包的 browser_collector.mjs 是可选适配器，不是所有 agent 的通用浏览器驱动。先读 host-compatibility.md 的能力检查与降级路径。

智联招聘平台的站内搜索和列表侧边详情按[智联站内搜索与整页核验](zhaopin-onsite.md)执行。下述共用脚本及 `process-browser` 当前按官网招聘来源处理，不直接把平台结果套入官网回执；平台辅助读取须保留实际 `source_channel=zhaopin`、`source_type=platform`。

## 准备

1. 保留用户配置和 `config_fingerprint`。先打开已知入口，观察列表、标题、分页、加载和空结果控件，记录 DOM 选择规则的来源。未知选择器/URL 不猜测；页面变化时重新观察并更新运行计划。
2. 根据本次观察建立计划（不能复用历史私有配置）：`entry_url`、`scope`、`card_selector`、`title_selector`、地点/企业选择器、`total_pattern`、`next_selector` 或 `page_selector`、详情字段、详情选择条件。该文件是一次运行的已观察规则，不是域名专用 Python 适配器。
3. `select_pattern` 只是选取详情的范围，不替代岗位匹配。完整保存所有列表行及是否选中；相邻财务岗位不能自动当成财务 BP。分页与列表总数须对账，局部关键词结果不得称全站穷尽。

## 执行

如宿主是 Codex，先按当前 `cua_repl` 工具提供的入口取得目标浏览器/标签，并阅读它返回的API文档。当前工具入口包括 `cua.getTab`、`cua.getBrowser`、`cua.createBrowserTab`；实际选择遵循工具说明和用户指定的浏览器。不要照抄历史的 `agent.browsers.get` 初始化方式。

下方脚本接入方式是2026-10-24已验证的接口契约：执行器需要具备 `tabs.new/list/get` 等能力的Browser句柄，以及 `goto`、`playwright` 等Tab方法。仅当当前运行时文档允许这些调用和模块导入、且返回兼容句柄时使用。工具接口变化时先核对兼容性；10月26日智联已使用当前API辅助读取，不等于共用官网脚本已重跑。接口不兼容则报告脚本接入缺口，使用当前允许的可见页面读取流程保存真实证据，不伪造browser-run回执。不要启动另一套 Playwright/CDP、读取 Cookie、调用内部接口或改变访问策略。

```javascript
var fs = await import('node:fs/promises');
var {pathToFileURL} = await import('node:url');
var reader = await import(pathToFileURL(SKILL_DIR + '/scripts/browser_collector.mjs').href); // SKILL_DIR 为实际安装目录
var plan = JSON.parse(await fs.readFile('本轮计划绝对路径','utf8'));
var collector = reader.createCollector(browser, plan, '唯一输出目录绝对路径');
var steps = collector.collect(plan.companies[0]);
await steps.next(); // 逐检查点推进，直到 done；每次一般1次有延迟的浏览器操作
```

对每家企业依次执行 `collect`。一次调用推进1—2步，工具超时留足余量并及时给用户反馈；不要在一个工具调用里无限循环。最后推进到 `done:true`，确认每家都有 finished 记录。每步自动保存 `browser-run.json`，勿手写成功状态或补造快照。进程被中断时可以处理已保存的部分回执，但本版本不提供自动跨会话续跑；重跑用新目录并报告范围。

同一会话修正选择规则后，可对已结束的企业使用 `collector.retry(spec)`，自动保留前次尝试和被替代详情，仅重跑该企业；不要用数组pop替代按企业名定位，避免误改其他企业记录。仍在running的生成器需先正常结束，再重试。

- **动态加载**：检查业务内容；空壳或 Loading 记录为等待，通过已观察的列表/详情定位器等待可见后再读取。仍未加载记 blocked/partial，不能把暂时的0条记为无岗位。
- **返回列表与标题**：返回列表仍为空壳时先等待已观察的岗位卡片，再比较恢复状态；数字分页同时识别页面实际使用的 selected 状态。编号职责/任职要求按章节边界提取；标题容器混入类别标签时，只有正文主标题存在独立精确岗位名才接受，仍拒绝不一致的详情。
- **iframe**：主体为空且只有一个带 ID 的可见 iframe 时自动进入，读取实际文档 URL 和可见链接；按 `navigation_text` 跟随原文招聘链接，可将该链接提升为独立文档。保留父页面和 iframe 快照。多 iframe、无 ID 或嵌套结构需要补充观察，不猜选。
- **新标签页**：点击前后比较标签清单，唯一新标签自动读取并关闭；同页详情返回后核对列表恢复。多个新标签不随意选取。详情须与点击的标题一致。
- **覆盖**：遍历已观察的分页直到末页，列表数和站点总数一致或有明确结束标志才标 complete。重复页、超时、达到显式页数上限保留 partial，不暗改预算。
- **访问限制**：验证码、明确拒绝和授权要求单独记录并按当前浏览器规则处理，不改代理、不关闭安全机制、不自动投递。

## 进入核心

```text
python -m jobfinder process-browser --config <原用户配置> --input <browser-run.json> --output outputs/real/<唯一目录> --channel <渠道>
```

`--channel` 必填或由回执的 `source_channel` 给出。缺省成 `official_career` 会把平台回执的来源、
预算与证据归属全部记错；平台回执必须是 `source_channel=zhaopin`/`linkedin`… + `source_type=platform`。
回执 `runtime` 允许 `codex_browser_visible_dom` 或 `jobfinder_local_browser`。

核心验证原配置指纹、每份快照的哈希、标题对应关系和逐字段原文；保留 `browser_visible_dom`、iframe/新标签轨迹、原始 URL 和读取时间。薪资未披露留空，投递按钮仅记录原详情页入口，开放状态默认 UNKNOWN。按现有 `deduplicate/evaluate/export_bundle` 生成 `results.json`、工程 `results.xlsx`、单张十列 `岗位汇总.xlsx` 和 `report.html`，更新资源库入口状态。

标题未命中但职责可能相关时，助手阅读完整原文并填写独立 JSON 数组，通过 `--role-reviews <文件>` 送入核心。每项含 `source_url`、原 `role_keywords`、`decision`（related/out_of_scope/unknown）、原文 `quote`、对应 `content_hash`、`rationale`。校验URL/配置/哈希/摘录均通过后才调用既有 role_review 判断；保留原标题，不靠自动关键词猜测语义，也不改变原配置指纹。

“职责与要求完整”与“用户所需字段都披露”“符合本次条件”“岗位仍开放”分别判断。浏览器动作数和快照数单列，Python HTTP请求为0，浏览器内部网络请求数未知；不声称本次修复了HTTP读取。原始证据不得自动改变本轮配置、来源权限或规则。

## 范围限制

当前自动化覆盖已观察的 DOM 列表、数字分页/下一页、唯一新标签、单 iframe 和标签明确的职责/要求。滚动虚拟列表、多层iframe、复杂交互搜索及站点DOM变化仍需重新观察。固定的最大页数是显式运行保护，命中上限须报告未完成。10月24日排障曾按用户授权不受原搜索次数限制；该次豁免不改变默认预算，继续任务遵循已有授权，新任务按本轮条件执行。

两轮12家验证中，6家采用脚本、6家为浏览器辅助读取；后者逐字段形成Job/Evidence并进入既有核心，不宣称全部由process-browser自动采集。辅助合并保留原始读取时间、旧记录来源及新旧计数。详情完整、列表覆盖和岗位匹配分别统计，见项目根目录BROWSER_COLLECTION_ACCEPTANCE.md。

## 与完成度的对应关系

浏览器回执是"批量采集信封"（`browser-run-1`），每家企业一条记录；判断"这条搜索链到底了没有"仍落到
统一的范围粒度上：

| 回执字段 | 对应控制器概念 |
|---|---|
| `companies[].coverage_complete=true` + 无 `problems` | 该企业这条链 `exhausted=true` |
| `companies[].coverage_complete=false` 或命中页数上限 | 该链仍开放，阶段不能关闭 |
| `companies[].problems` / `parse_errors` | 记缺口（`blocked`），不写成"零岗位" |
| `receipt_version` | 默认 `receipt-1`（采集信封）；新写入建议显式给 `receipt-2` 并在 `coverage_complete` 里承担"到底"语义 |

`process-browser` 只更新与该回执 `channel` 相同的任务；不会把平台回执记到官网任务上。
阶段性导出同样按整理后的 `verdict` 命名文件。
