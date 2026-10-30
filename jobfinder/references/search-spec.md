# 统一输入与匹配契约

唯一数据定义：项目根目录 `jobfinder/spec.py` 的 JobSearchSpec。学历事实与比较：`jobfinder/education.py`。最终 MATCH / REVIEW / EXCLUDED：`jobfinder/matching.py:evaluate`。Skill、网页、提取 Prompt 均不另写一套核心判断。

新任务规则 CN_MAINLAND-4.0。用户原文保存在 raw_input；规范化有效条件保存在 effective_search_spec。包含目标地点、岗位范围/词组、本人学历/经验、薪资、招聘类型、渠道、搜索/页面/模型/运行预算、补充要求、已启用搜索服务及重试策略。网页 input/config 是兼容投影，恢复和导出以保存的有效条件为准。调预算只更新执行条件及预算历史，不改 raw_input。

CLI 接受标准 JobSearchSpec JSON，也接受当前 `configs/user.template.json` 的核心配置；后者自动规范化到同一模型。执行前用 `python -m jobfinder validate-config --config <配置>` 查看 effective_search_spec，plan 也保存它。完整 schema 可从 `JobSearchSpec.model_json_schema()` 生成，无需在文档复制枚举。

例如本人本科：`rules_version=CN_MAINLAND-4.0`，`candidate_education=BACHELOR`。未设置为 null。自动比较本人学历能否满足原文最低门槛；高级“岗位学历要求”本轮未增加新 UI，不混用旧下拉标签。岗位保留 education_raw，并由程序计算 minimum_education；学历缺失、只有“本科优先”或条件性放宽不能冒充学历不限。专业、毕业年份等未建模条件保留原文待核验。

无版本的历史配置按 CN_MAINLAND-3.9 读取，保存 legacy_matching_config，完全保留原来的门槛比较方向与不限选项，不从“本科及以上”反推用户学历。旧任务首次普通更新时补存契约，读取时不批量重写数据库。旧配置指纹保持兼容；新任务不使用旧简写。

模型提取原文事实，不按学历或经验提前丢岗位，也不提供最终匹配结论。描述、学历、经验、地点、薪资必须绑定原文；原文没有就留空。规则引擎随后统一判断。搜索策略、浏览证据与职能边界仍由本 Skill 负责。

网页版自动备用搜索、尝试账本与服务健康状态由 `jobfinder_web/search_execution.py` 执行。通用 Skill 的工具执行由助手调度，须按同一有效预算逐次记账；不能声称 CLI 会启动网页版搜索服务或复用会话内部工具。
