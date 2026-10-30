# 企业招聘入口复用与回退（3.6建立，3.9继续兼容）

当前默认每城40次实际搜索，企业/HTTP不限（null）；有限配置继续兼容。所有回退共享原搜索次数，不重置已用量。按城市/候选轮转与每步保存详见[预算契约](budget.md)。

本契约只调整企业官网发现路线，不放宽其他渠道规则、网络安全或访问权限。取消额外的官方身份认证业务步骤；企业名称、岗位实际城市、岗位范围和原文归属仍须核对。办公城市只能影响企业查询优先级，不能补成岗位工作地点。

## 顺序与职责

1. 官网渠道的企业名单只来自**本渠道的岗位查询词/研究型检索结果**（`official-catalog`）或用户显式提供的名单；平台渠道发现的企业不进入。名单确定后，先按完整规范化名称匹配知识库。CLI默认只读匹配data/resources/companies.sqlite3（存在时）；显式--knowledge-base也可将库作为企业选择来源。品牌和子公司不自动合并。
2. 有招聘入口或适用的历史详情页，生成entry_tasks，先按当前策略读取；不例行生成该企业查询，不先访问首页。适用详情须已有岗位标题、实际城市证据；历史可用不代表本次仍可用。旧链接无检查状态时标unknown，本次检查，不批量迁移历史文件。
3. 只有官网，生成discover_from_home读取任务，从已读取页面的真实招聘导航发现入口。未审核的目标标policy_pending；不会因为文字像招聘就自动放行。复杂动态入口可由助手在既有工具权限下处理。
4. 没有入口，或入口路径尝试后仍未取得对应岗位线索，生成合并查询“企业名称 城市 岗位词 招聘”。不增加独立官网定位/官方身份验证查询。直接城市岗位查询仍保留原规则，它不等于企业例行搜索；**目录查询（监管企业名录/机构名单）已删除**，目录页不是岗位来源，不再作为官网发现路径。

## 官网渠道的企业名单（4.1）

名单不是从目录页读来的，也不是从平台渠道抄来的，而是**本渠道岗位查询词/研究型检索结果里出现的雇主**。三步走，每步都有文件可查：

```powershell
# 1) 执行 planned 岗位查询词任务，保存回执
# 2) 回执 -> 企业名单（按首次出现顺序，剔除平台域名/目录页/无企业名/无任务归属）
python -m jobfinder official-catalog --plan data/discovery/<计划>.json --receipt <回执>.json --output data/discovery/<名单>.json
# 3) 名单 -> 逐家企业查询任务（按名单顺序，超预算标 budget_exhausted）
python -m jobfinder official-plan --config <同一配置> --plan data/discovery/<计划>.json --catalog data/discovery/<名单>.json --output data/discovery/<新版计划>.json
```

约束：

- **顺序即发现顺序**。不按名称或薪资重排，名单里 `discovery_order` 就是检索顺序。
- **渠道独立**。名单只读本渠道回执；`task_ids` 不命中本渠道查询任务、链接落在招聘平台域名、候选类型是 `enterprise_directory` 的，一律剔除并在 `excluded` 留痕。平台渠道（智联/猎聘/BOSS/前程无忧）发现的企业不得回填本渠道，本渠道名单也不得回填平台渠道。
- **代称不入名单**。企业名含"某/猎头/代招/保密/匿名/一家/多家"的按 `placeholder_employer` 剔除——"某上海世界500强公司"生成不出可执行的官网检索任务。
- **预算不足不删行**。超出每城搜索次数的企业保留为 `budget_exhausted`，与已计划企业分开统计，报告里要写清"名单 N 家、实查 M 家、超预算 K 家"。
- **名单不等于岗位**。企业进了名单只说明它在查询结果里出现过，是否在招、岗位是否在目标城市，仍须在官网原文核实。

## 官网渠道的研究型检索（4.2）

除了 `direct` 查询词任务，计划里还有 `route=research` 任务，交给托管研究型检索（`domain_search`）执行：一次能跨十几个来源出"哪些企业正在招这个岗位"，比通用搜索的 5 条上限覆盖广得多。它解决的是"官网渠道拿不到企业名"这个卡点。

规则：

- **只认本渠道任务归属**。research 候选的 `task_ids` 必须指向官网渠道的 research 任务，否则一律 `not_official_channel_task`。
- **平台域名有豁免，但要标 lead**。研究型摘要常指向平台页面；这类候选允许进名单，但写入 `lead_only=true`、`verification_required=true`，`source_url` 保留原链接作为线索出处。**它不是证据**，必须回官网核实企业全称与在招状态后才能进交付表。
- **官网域名不标 lead**。研究结果直接命中的企业官网页面，`lead_only=false`，正常走入口读取。
- **`direct` 任务的豁免不扩大**。同样落在平台页面的候选，如果挂在 `direct` 任务上，仍然按 `platform_url` 剔除。
- **摘要不是原文**。研究型检索输出的是合成叙述，岗位条目不能直接入库；只能用来产企业名，岗位仍要逐条读原文。

Python执行入口读取、已审核导航回退、原文解析与evaluate；在线搜索仍由助手用获准工具执行。返回follow-up-plan.json只表示后续计划，不能报告已搜索。动态外壳、无法解析和访问受限分别报告，不冒充无岗位。

## 计划、回执与运行

plan/extend-plan输出tasks（搜索）和entry_tasks（读取），两者不混计搜索次数。entry_tasks含task_id、company、target_city、action、status、reason和可直接校验的Candidate；超企业限额标budget_exhausted。原有role_groups/company_assessments/筛选排序继续有效。

运行结果discovery.entry_log记录每个读取任务的实际状态/原因，reports.entry_trace保留逐URL尝试和运行内复用，matched_cities及fallback_target_cities区分已覆盖和仍需查询的城市。某城市查到岗位不能取消另一城市的回退；没有实际请求的预算/策略待审状态不作为跨城市终止失败缓存。逐URL报告保留真实访问状态，回退成功不会把旧坏链接改写成读取成功。

助手执行planned搜索任务，保存正常codex_assisted回执。仅先读入口时，可提供本计划关联的空回执：mode=codex_assisted、正确plan_id/config_fingerprint、search_log=[]、candidates=[]。空回执不表示搜索成功；未执行搜索在报告中为not_run。调用new-search时Python自动读取计划内planned entry_tasks，不要再把它们复制为“搜索发现”候选。

```powershell
python -m jobfinder plan --config <配置> --catalog <名单> --output data/discovery/<计划>.json
python -m jobfinder new-search --config <同一配置> --plan <计划> --candidates <回执> --policies <策略> --output outputs/real/<独立目录>
```

若输出follow-up-plan.json，检查其中planned任务，执行实际合并搜索、创建该后续plan_id对应的新回执，再以该文件作为--plan调用new-search。不得重新build_plan来抹去本轮消耗。后续计划携带budget_used.searches/requests/companies及query_history；查询、robots、导航、失败、重试和回退均沿用原预算。没有剩余搜索或读取预算标budget_exhausted，不无限加查。已尝试的相同合并查询不再追加；运行内HTTP地址（去片段）共用成功/失败缓存，避免重复访问与回退循环。

## 失效、受限及无结果

| 观察 | 状态及动作 |
|---|---|
| HTTP404／410 | invalid；尝试已知父招聘入口、官网中其他已审核招聘导航，仍无可用线索才输出合并搜索计划 |
| 单个岗位关闭或原文截止日期已过 | 只将该job_detail记job_closed；招聘入口仍可用，继续其他岗位/入口 |
| 登录、401／403／429、验证码、robots限制 | restricted；不绕过、不删除旧链接、不提前重试429；可尝试其他已审核公开入口 |
| 超时、TLS错误、暂时服务错误 | temporary_failure；原重试上限不变，保留旧链接，可走其他公开入口 |
| 未审核或预算不足 | not_checked；保留既有可用性并记录last_check_status；不得记已检查或永久失效 |
| 成功搜索无候选／本次未取得匹配岗位 | not_found_this_run或incomplete；仅描述本次覆盖，不能推断企业没有岗位 |

入口可读、详情解析、岗位开放和条件匹配分别记录。读到其他招聘主体时保留原文主体并标归属冲突，不自动当作本企业岗位。

## 来源与增量知识库

Candidate.source_type及Job.source_type区分official_website、recruitment_provider、platform、repost、unknown；来源渠道source_channel保持。助手据已知资料/实际页面填写类型和source_evidence，不能仅凭搜索摘要自称官网。原站指向不同域招聘入口时记录home_navigation关系及来源页哈希；跨域本身不能证明是招聘服务商，已审核目标策略可另填source_type，未分类保持unknown。本站及同属已知官网的招聘子域可沿用官网类型。这不是额外认证查询，也不授予目标域访问权限。

资源快照schema_version=1.2，兼容原三表。discoveries追加kind=entry_check，其context.entry保存url、entry_type、source_type、source_channel、source_url、checked_at、availability、reason、evidence；详情另存title/cities。records/快照entries投影最新状态，历史检查仍在discoveries。按(URL,entry_type)分别记录，同一页既是岗位列表又含关闭岗位时不会把列表标坏。

official_url/career_url仍按现有逐字段证据及冲突规则追加，不覆盖/删除旧值。platform/repost候选不会把自己的链接写入这两个企业字段；详情原文写入真实招聘主体。已有字段冲突不静默合并，入口来源关系单独留证。临时失败不清空业务资料或旧链接。旧库只读规划不写快照；正常运行只增量更新本轮涉及的企业。

合成示例：configs/example.entry-flow.json。离线测试：tests/test_entry_flow.py。示例不是用户条件，也不提供真实站点访问授权。

## 进入栏目后的遍历

规则3.9按[遍历契约](traversal.md)继续列表分页和相关详情。找到一条匹配岗位不代表栏目已完成；入口回退和后续搜索计划不替代未完成的遍历进度。
