# 岗位词组：规则与结构化契约

当前版本：role-groups-2，2026-10-17。兼容role-groups-1：旧组未填性质时为unclear并说明未判断，不自动补专业分类。项目根目录为本文件向上四级。本契约是独立的初始展开产物，不是SearchConfig、查询计划或真实岗位结果。历史outputs内的role-expansion.json保持当时格式，不迁移、不覆盖。

## 展开方法与边界

1. 原样保存用户需求，提取每个明确岗位为explicit_inputs；写清scope_boundary和明确排除的职能。判断breadth为narrow或broad，不因结果少自行放宽。用户只说“三维动画师”时不能扩到二维；“动画制作”才可能覆盖二维、三维等制作方向。
2. **先列岗位名称，再分组**：每组只表示同一实际职能。主名称选能独立用于搜索的岗位名称，别名是同一职能的常见名称、可靠中英文名称或缩写。二维动画师/2D动画师可同组；三维动画师/3D动画师另组。不能因同属动画行业或共享工具就归同组；Spine、Maya、游戏、MCN等裸工具/行业词不自动视为岗位同义词。工具限定的岗位名仍须判断实际职能，不机械新增一组。
3. category记录该组与原需求的关系：synonym为原岗位本身或同义名称；specialization为输入范围内同职能细分；adjacent为职责可能相交的相邻职能。别名本身始终是组内同职能名称，不用“相邻”关系塞入别名。
4. 范围内的synonym/specialization放ready_groups；范围内但须核验具体职责的adjacent放conditional_groups；超范围或范围不清的组放pending_confirmation_groups，写明所需用户确认。用户明确排除的职能不主动展开；范围变更须有用户依据。即使用户同意相邻方向，岗位是否满足职责仍须原文核验。
5. 每组写rationale及verification_conditions。向用户简要展示分组、依据和待确认项；常见可靠同义词不追问。原文不足的条件组保持待核验，不凭标题或摘要升级为匹配。
6. 每组写nature.kind、nature.rationale；kind为specialist/general/mixed/unclear，按实际工作内容判断，不按标题词典硬分类。专业组须填写nature.company_requirements（工作需要的业务、产品或部门）；通用职能不按雇主行业硬排除。混合/不明保守待核查，不因此改变组的范围或确认状态。判断由助手负责，Python不验证语义。

## 初始规模与规范化

- 窄岗位通常1–3组，单批最多4组；宽职能通常3–5组，单批最多6组。上限统计三类组的总数，不要求达到通常数量。每组1个主名称，最多2个去重后的常用别名；不确定时仅原词也可。
- 这是初始自动展开的限制，不是搜索次数或用户岗位数量限制。用户明确指定超过上限的岗位仍全部记入explicit_inputs；当前批无法处理的整项放deferred_inputs并注明原因，可后续分批。不能把用户明确岗位当冗余删除，也不能通过增加分组放宽职能边界。
- 展示文字折叠首尾/连续空白；比较键用casefold忽略大小写，保留首次出现的拼写和顺序。不改写original_request与raw_text。组内主词/别名完全重复只留首次；同bucket、同ID、所有规范化字段完全相同的重复组只留首次。
- 不同组出现相同规范词或重复ID但内容不同，校验报错，交由助手明确职能后重写；不通过同义猜测自动合并。不要为通过校验生造差异词。
- input_id和group_id由编写者初次分配，格式为小写字母开头、其后小写字母/数字/连字符/下划线，最长64字符。新增别名、调整显示名称或分批引用时沿用同一职能的group_id；实际职能拆分才建新ID并保留原记录。ID不是每次随机生成或根据别名重新计算。

## 字段契约

| 字段 | 含义 |
|---|---|
| schema_version | 新建用role-groups-2，兼容role-groups-1；与匹配规则版本独立 |
| artifact_kind | example或user；示例不得冒充用户实际条件 |
| original_request | 用户原话；示例须明确标示例 |
| breadth / scope_boundary / excluded_functions | narrow/broad、明确职能边界、排除职能列表 |
| explicit_inputs | 非空数组，每项input_id、raw_text；保留所有明确岗位 |
| ready_groups | 可直接用于发现候选的范围内同义/细分组，非岗位匹配保证 |
| conditional_groups | 范围内相邻职能，须职责证据；不进入默认search_groups |
| pending_confirmation_groups | 范围不清/超范围，须用户确认；不进入默认search_groups |
| deferred_inputs | 每项input_id、reason，表示整项等待后续批处理 |

三类组共用字段：group_id、source_input_ids（非空输入ID列表）、primary_term、aliases（最多2个唯一别名）、category（synonym/specialization/adjacent）、scope_relation（within/outside/unclear）、rationale、verification_conditions（非空文字列表）、nature（kind、rationale、company_requirements）。新版每组须显式给性质和非空理由，专业组的company_requirements非空。原词无别名时aliases=[]。没有日期、城市、预算、查询字符串或role_keywords字段；额外字段报错。

每个explicit_inputs条目必须被至少一个组引用，或被deferred_inputs明确覆盖，不能同时标为已分组和整项待处理。校验器检查已填写ID的完整性，**不能判断助手是否从原话完整提取所有岗位、名称是否真同义、分类/范围标记是否语义正确或确认是否真实发生**。这些须助手核对原始需求、用户回复或原文职责；不能把structure_valid解释为业务正确。

## 本地使用与示例

从项目根目录运行（只读输入，标准输出JSON，成功0、无效2）：

```powershell
python -m jobfinder.role_expansion --input configs/example.role-expansion.animation.json
```

实现为jobfinder/role_expansion.py中的RoleExpansion及main。输出status=structure_valid、semantic_validation=not_performed、规范化expansion和search_groups。search_groups只含ready组的group_id、primary_term、aliases，顺序稳定，不把词合成查询、不附加AND/OR、不执行搜索，也不写入SearchConfig。助手保存原始输入和规范化产物，不能用后者覆盖原话。

需要复用规范化产物时，将输出中的expansion对象另存为新JSON；status、semantic_validation及search_groups属于校验报告包装，不是输入契约字段。

示例configs/example.role-expansion.animation.json明确标为example，非本轮真实求职需求：

| 稳定ID | 主名 | 别名 | 类别/处理 |
|---|---|---|---|
| animation-2d | 二维动画师 | 2D动画师、2D Animator | 同职能细分，可直接搜索 |
| animation-3d | 三维动画师 | 3D动画师、3D Animator | 同职能细分，可直接搜索 |
| animation-storyboard | 动画分镜师 | 分镜师、Storyboard Artist | 同职能细分，可直接搜索；核对动画用途 |
| animation-motion | 动效设计师 | Motion Designer | 相邻职能，待原文职责核验 |
| animation-game-vfx | 游戏特效设计师 | 无 | 范围不清，待用户确认及职责核验 |

此示例的宽范围允许动画分镜；不表示所有“动画师”需求都包含分镜或特效。若原始需求限定三维，应以三维组为主，按实际范围选择细分，不能照搬本例。

## 后续搜索和匹配的交接边界

词组供后续搜索逐词使用，或经确认渠道支持后用OR；不要把所有别名拼成必须同时命中的条件。关联记录使用group_id追溯来源。plan/extend-plan已通过--role-groups消费性质以筛选企业，不改变原role_keywords查询用词；当前每城40次默认与轮转遵循[预算契约](budget.md)。只使用ready组且能关联当前配置岗位词或explicit_inputs的组；未覆盖的当前岗位保留unclear，不用其他组代替。CLI输出role_context_hash绑定完整规范化词组，企业判断须引用此哈希，变化后重新判断。详情见[企业筛选契约](company-selection.md)。

相邻/待确认组不能无条件加入role_keywords，已核验的词也须结合具体岗位职责证据。现有evaluate仍只按原标题子串进行岗位维度匹配，无职责语义分类、排除词引擎或自动证据晋级。宽泛词可能误收无关标题，不能把结构校验或标题命中当作职责符合。

以后读取原文发现新别名，可记录依据并保留组ID；超出每组初始别名限制时显式选择/分批，不静默丢弃用户指定项。改变role_keywords会改变配置指纹，新搜索不能沿用不一致的旧计划/回执。搜索结束才确认的别名另存matching-config及与search-config差异，仅重新匹配，保留历史搜索指纹及原标题。此次不实现自动预算联动或自动回写历史产物。
