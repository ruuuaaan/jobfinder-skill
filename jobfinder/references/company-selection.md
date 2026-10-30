# 企业资料与相关性选择

2026-10-17，CN_MAINLAND-3.5。Python接入结构校验、证据归属检查、确定性排序和企业限额；助手负责实际语义判断、获准网页搜索/浏览器原文读取。规划器不联网、不推断语义、不证明资料仍有效。

## 企业资料

资源库保留原三表，JSON快照版本1.1。company_intro为有明确主体的原文介绍，main_business为明确业务，industry仅明确行业标签。旧业务不自动复制为简介。operating_locations为[{city,unit_name,kind}]，kind仅office/branch/factory/store/research_center/operating_site；不写注册地或总部，也不当成岗位实际地点。hiring_history为[{title,cities}]，仅历史查询线索，不保证现在在招。

正常service读取官网、已提供介绍页及已验证招聘页时顺便提取明确语句，不额外爬页。自动提取有意保守，多主体、别名或复杂版式由助手核对后填协议。解析出的岗位按原文雇主入库，不能归给候选集团。

浏览器/网页工具原文用company-facts-1：顶层schema_version、artifact_kind（user/example）、facts。每项company_name、field、value、evidence。evidence包括entity_name、source_url、observed_at（含时区）、quote、content_hash、kind（profile/job/operating_unit/explicit_scope）、subject_relation（self/operating_unit/parent/subsidiary/headquarters/registration）。简介、业务及招聘须属于本企业；经营机构可为本企业实际机构，子公司独立记录。注册地/总部证据不用于经营机构。

`python -m jobfinder record-company-facts --input <本轮原文资料JSON>`先校验整批，再增量写固定企业库和快照。example拒绝写入。空值不清空旧字段，失败不伪造空资料；不同简介/业务值保留证据并标conflict。不同经营机构可累积，同名同类型机构城市矛盾则待核查；多条历史岗位累积。来源时间和最后核验时间不表示资料生效日期或永久有效。

## 助手填写企业判断

先读取完整名单和已有库。专业岗位按nature.company_requirements核对实际业务、产品或部门；通用岗位不因行业不同排除。两类都核对目标城市的岗位或实际机构；总部在外地、网页未提及、没查到岗位都不是排除证据。历史岗位仅提高查询优先级。确需补资料时每企业至多补1个明确导航的官网介绍页，使用原请求预算；未解决的保持review，不为筛选先爬全站。

company-assessments-1顶层为schema_version、role_context_hash（岗位词组校验CLI输出）、assessments。每项company_name、group_id、target_city、dimension（role/business/city）、verdict（related/out_of_scope）、rationale、coverage（partial/explicit_scope）、非空evidence数组；证据结构同上。

- role=related须具体岗位原文；city=related须岗位地点或经营机构原文。按实际职能判断，不能因“动画”二字出现就声明岗位相关。
- out_of_scope须coverage=explicit_scope且证据kind=explicit_scope，原文须明确覆盖所判断范围；一条无关岗位不能推出全企业没有相关岗位。主体须精确归属，不继承集团/子公司、注册地或总部属性。
- 只引用关联本轮输入的ready组；条件/待确认组不参与自动排除。多个岗位组按任一可查保留，只有全部可用组被明确排除才排除企业。性质或范围变化使哈希不同，旧判断拒绝复用。

Python复用库中有原文证据的精确岗位名称（主词/别名完全匹配，仅作线索）和经营城市；复杂职能及业务关联由助手判断。缺少判断默认review，标题/行业关键词不是完整分类引擎。

## 接入与审阅

当前3.9默认企业数量不限（null），仅指本轮发现/选取的相关企业；岗位和城市证据筛选继续生效，显式有限配置仍按限额。只来自知识库且相关性不足的review企业记needs_scope_selection，不自动扫整库；先由本轮名单/明确选择纳入，或补足岗位/城市证据。归一企业名去重，不扩展关联企业。

```powershell
python -m jobfinder plan --config <本轮配置> --role-groups <词组JSON> --catalog <完整名单JSON> --catalog-source listed_companies --knowledge-base data/resources/companies.sqlite3 --company-assessments <企业判断JSON> --output data/discovery/<新计划>.json
```

名录用enterprise_directory，上市名单用listed_companies，官网渠道从岗位查询词结果生成的名录用query_discovery；知识库可单独用--knowledge-base（SQLite或完整JSON快照）。三个来源共用筛选；读取SQLite不建库、不写快照。导入政府HTML默认解析已提供页面全部可识别企业；--limit仅为明确抽样，抽样不等于完成相关性筛选。

用户明确指定企业通过重复--company传入，不从网页或名单标志推断。指定企业优先保留，超企业数量限制仍生成budget_exhausted入口。其余依次为：直接岗位证据、业务关联且有城市机构、通用职能且有城市机构、业务关联但城市待查、其他待核查；同档按规范化名称排序。冲突不用于排除。先评估完整输入再限额；未选中记录deferred_company_limit，不等于不相关。

plan.company_selection保存全部逐城/逐组状态、理由、证据、来源、role_context_hash和输入摘要哈希；任务关联选择决定。extend-plan沿用岗位性质，只选新企业并扣除已计划企业数，不重排已执行任务；新报告留在company_selection_history。企业线索显式target_cities仅限制本批发现范围，不证明其他城市无岗位。

岗位查询及搜索预算不变，执行后才算搜索；自动公告模板保持删除。访问策略不放宽。岗位MATCH仍由原evaluate产生。合成完整示例见configs/example.company-relevance.json，包含配置、词组、名单、判断和资料，仅用于结构与离线验收。

## 选择后的入口读取（规则3.6）

企业名单确定后按[入口契约](entry-flow.md)匹配知识库。选中企业可生成entry_tasks而非搜索tasks；旧数据兼容为待检查，CLI默认本地库只作所选企业入口匹配，不借此扩大名单。没有可用入口才生成企业名称＋城市＋岗位词＋招聘的合并查询，不追加身份认证。企业经营城市仍不能替代岗位实际地点。
