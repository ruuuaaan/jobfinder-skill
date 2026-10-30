# 可见浏览器自动采集

适用于官方招聘站只返回标题、JavaScript 空壳、iframe，或职位点击后进入新标签页的情况。使用宿主当前浏览器工具读取实际可见页面。独立 Python CLI 只验证和处理回执；随包的 browser_collector.mjs 是可选适配器，不是所有 agent 的通用浏览器驱动。先读 host-compatibility.md 的能力检查与降级路径。

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

```powershell
python -m jobfinder process-browser --config <原用户配置> --input <browser-run.json> --output outputs/real/<唯一目录>
```

核心验证原配置指纹、每份快照的哈希、标题对应关系和逐字段原文；保留 `browser_visible_dom`、iframe/新标签轨迹、原始 URL 和读取时间。薪资未披露留空，投递按钮仅记录原详情页入口，开放状态默认 UNKNOWN。按现有 `deduplicate/evaluate/export_bundle` 生成 `results.json`、工程 `results.xlsx`、单张十列 `岗位汇总.xlsx` 和 `report.html`，更新资源库入口状态。

标题未命中但职责可能相关时，助手阅读完整原文并填写独立 JSON 数组，通过 `--role-reviews <文件>` 送入核心。每项含 `source_url`、原 `role_keywords`、`decision`（related/out_of_scope/unknown）、原文 `quote`、对应 `content_hash`、`rationale`。校验URL/配置/哈希/摘录均通过后才调用既有 role_review 判断；保留原标题，不靠自动关键词猜测语义，也不改变原配置指纹。

“职责与要求完整”与“用户所需字段都披露”“符合本次条件”“岗位仍开放”分别判断。浏览器动作数和快照数单列，Python HTTP请求为0，浏览器内部网络请求数未知；不声称本次修复了HTTP读取。原始证据不得自动改变本轮配置、来源权限或规则。

## 范围限制

当前自动化覆盖已观察的 DOM 列表、数字分页/下一页、唯一新标签、单 iframe 和标签明确的职责/要求。滚动虚拟列表、多层iframe、复杂交互搜索及站点DOM变化仍需重新观察。固定的最大页数是显式运行保护，命中上限须报告未完成。10月24日排障曾按用户授权不受原搜索次数限制；该次豁免不改变默认预算，继续任务遵循已有授权，新任务按本轮条件执行。

两轮12家验证中，6家采用脚本、6家为浏览器辅助读取；后者逐字段形成Job/Evidence并进入既有核心，不宣称全部由process-browser自动采集。辅助合并保留原始读取时间、旧记录来源及新旧计数。详情完整、列表覆盖和岗位匹配分别统计，见项目根目录BROWSER_COLLECTION_ACCEPTANCE.md。
