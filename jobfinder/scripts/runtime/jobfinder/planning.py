"""Pure query planning. Search execution belongs to an explicitly configured provider."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from .geography import normalize_city
from .models import RULE_VERSION, now
from .input_rules import recruitment_terms
from .company_selection import select_companies
from .role_expansion import RoleExpansion
from .matching import synonyms_for
from .entries import entry_task
from .workflow import channel_stage, PROTOCOL_VERSION
from . import receipts
from .channels import BY_NAME as CHANNEL_SPECS
from . import channels as channel_registry

OFFICIAL = 'official_career'
MANUAL = 'manual_import'
RESEARCH = 'research'
BROWSE = 'browse_list'
# 渠道检索方式：只用于生成可核对的查询标签，真正的城市参数在渠道契约里。
# 名称统一取自 channels.py，避免菜单、规划、导出三份名单各自维护。
CHANNEL_METHODS = {name: (spec.label, '公众号检索' if name == 'bendibao_wechat' else
                          '站内检索' if name in ('campus_job', 'bendibao_web', 'linkedin')
                          else '站内搜索')
                   for name, spec in CHANNEL_SPECS.items() if spec.supported}

# 列表页直读：平台渠道的主路径。
# 站内搜索受登录态、虚拟滚动、反爬白屏影响，一屏只出十几张卡片；服务端渲染的职能
# 分类页 / 城市聚合页一次能读出 20–40 条带薪资的岗位。只收录实测通过的站点与类目，
# 未收录的渠道/城市/岗位词自动退回站内搜索——不猜 URL，猜出来的链接会成为假证据。
BROWSE_CITIES = {
    '上海': {'slug': 'shanghai', 'code': 'sh'},
    # 2026-10-31 实测：jobs.51job.com/guangzhou/caiwufenxijingli/ 标题为「广州财务分析经理/主管招聘」，
    # 首屏 20 条带薪资、地点、学历、经验、发布日与详情直链；p2 另 20 条；p3 起无有效卡片。
    '广州': {'slug': 'guangzhou', 'code': 'gz'},
    # 2026-11-03 实测（北京）：jobs.51job.com/beijing/xinzifulijingli/p1/ 返回 200，但正文被阿里云 WAF
    # 的 JS 挑战挡住（curl 只拿到 renderData 空壳），必须用宿主浏览器读取；猎聘
    # www.liepin.com/city-bj/career/xinchoujixiaozongjian/ 服务端渲染 40 张卡片，含薪资/经验/学历/岗位id。
    '北京': {'slug': 'beijing', 'code': 'bj'},
    # 2026-11-03 实测（七城）：liepin 城市码不统一——广州/深圳/杭州/南京用两位缩写（gz/sz/hz/nj），
    # 武汉/苏州/宁波必须用全拼（wuhan/suzhou/ningbo，短码 wh/su/nb 均 404）。
    # 各城 city-<code>/career/xinchoujixiaozongjian/ 服务端渲染 34–40 张卡片；前程无忧 wenku 已整体触发 WAF。
    '广州': {'slug': 'guangzhou', 'code': 'gz'},
    '深圳': {'slug': 'shenzhen', 'code': 'sz'},
    '杭州': {'slug': 'hangzhou', 'code': 'hz'},
    '武汉': {'slug': 'wuhan', 'code': 'wuhan'},
    '南京': {'slug': 'nanjing', 'code': 'nj'},
    '苏州': {'slug': 'suzhou', 'code': 'suzhou'},
    '宁波': {'slug': 'ningbo', 'code': 'ningbo'},
}

BROWSE_CATALOG = {
    '51job': {
        'label': '前程无忧职能分类页',
        'url': 'https://jobs.51job.com/{slug}/{category}/',
        'page_url': '{base}p{page}/',
        'pages': 5,
        'categories': [
            (('薪酬',), 'xinzifulijingli', '薪酬福利经理'),
            # 2026-10-31 实测：上海 jobs.51job.com/shanghai/caiwufenxijingli/ 标题为
            # 「上海财务分析经理/主管招聘」，p1-p4 有卡片、p5 为空，含财务BP/Finance BP 原文。
            (('财务BP',), 'caiwufenxijingli', '财务分析经理/主管'),
            # 2026-10-31 实测（广州）：jobs.51job.com/guangzhou/ruanjianceshi/ 标题为
            # 「广州软件测试招聘」，p1/p2 各 20 条带薪资、地点、学历、经验、发布日与详情直链。
            (('软件测试',), 'ruanjianceshi', '软件测试'),
            # 2026-10-31 实测（广州）：jobs.51job.com/guangzhou/zidonghuaceshi/ 标题为
            # 「广州自动化测试招聘」，含自动化/测试开发岗位原文。ceshikaifagongchengshi 为 404，不收录。
            (('自动化测试',), 'zidonghuaceshi', '自动化测试'),
            # 2026-10-31 实测（广州）：jobs.51job.com/guangzhou/renliziyuan/ 标题为「广州人力资源招聘」，
            # p1/p2 各 20 条带薪资、地点、学历、经验、发布日、详情直链与职责原文；类目本身覆盖人事/招聘/
            # 薪酬绩效/培训/HRBP 等全部HR子职能，多个岗位词命中同一类目时由 browse_rows 合并为一条任务。
            (('人力',), 'renliziyuan', '人力资源'),
            (('人事',), 'renliziyuan', '人力资源'),
            (('招聘',), 'renliziyuan', '人力资源'),
            (('培训',), 'renliziyuan', '人力资源'),
            (('绩效',), 'renliziyuan', '人力资源'),
            (('HR',), 'renliziyuan', '人力资源'),
        ],
    },
    'liepin': {
        'label': '猎聘城市聚合页',
        'url': 'https://www.liepin.com/city-{code}/career/{category}/',
        'page_url': None,          # 虚拟滚动，只有第一页可读
        'pages': 1,
        'categories': [
            (('薪酬', '总监'), 'xinchoujixiaozongjian', '薪酬绩效总监'),
            # 2026-11-03 实测（北京）：liepin 没有「薪酬经理」独立类目（xinchoujingli 等 7 个候选 slug 均 404），
            # 经理档退回人力资源聚合页 city-bj/career/renliziyuan/ —— 41 张卡片，薪酬岗按标题再筛。
            (('薪酬',), 'renliziyuan', '人力资源'),
            # 2026-10-31 实测（广州）：www.liepin.com/city-gz/career/ruanjianceshi/ 单页 40+ 张卡片，
            # 带岗位/公司/薪资/地点/经验/学历；大量猎头代发匿名岗，只作岗位线索，不入企业库。
            (('软件测试',), 'ruanjianceshi', '软件测试'),
            # 2026-10-31 实测（广州）：www.liepin.com/city-gz/career/renliziyuan/ 单页约 40 张卡片，
            # 带岗位名/公司/薪资/地点/经验/学历与详情链接；虚拟滚动不可翻页，只读首屏。
            (('人力',), 'renliziyuan', '人力资源'),
            (('人事',), 'renliziyuan', '人力资源'),
            (('招聘',), 'renliziyuan', '人力资源'),
            (('培训',), 'renliziyuan', '人力资源'),
            (('绩效',), 'renliziyuan', '人力资源'),
            (('HR',), 'renliziyuan', '人力资源'),
        ],
    },
}


def browse_category(channel, role):
    """按岗位词命中已实测的类目；未命中返回 None（退回站内搜索）。"""
    target = BROWSE_CATALOG.get(channel)
    if not target:
        return None
    for keywords, slug, name in target['categories']:
        if all(k in role for k in keywords):
            return slug, name
    return None


def browse_rows(city, roles, channels):
    """展开列表页任务：一页一条任务，页号外层循环，让预算先覆盖到每个岗位词。

    同一渠道下多个岗位词命中同一类目时合并成一条（covers_roles 留痕），
    否则会生成两条完全相同的 URL 任务。

    未适配的城市直接返回空列表——调用方必须据此回退到站内搜索，
    不能因为"岗位词命中类目"就取消回退（那会让该渠道彻底缺失）。
    """
    meta = BROWSE_CITIES.get(city)
    if not meta:
        return []
    rows = []
    for channel in channels:
        target = BROWSE_CATALOG.get(channel)
        if not target:
            continue
        merged = {}
        for role in roles:
            hit = browse_category(channel, role)
            if not hit:
                continue
            base = target['url'].format(slug=meta['slug'], code=meta['code'], category=hit[0])
            row = merged.setdefault(base, {'base': base, 'role': role, 'covers': [],
                                           'category': hit[1], 'label': target['label']})
            row['covers'].append(role)
        for page in range(1, target['pages'] + 1):
            for row in merged.values():
                base = row['base']
                url = (target['page_url'].format(base=base, page=page)
                       if target['page_url'] and page > 1 else base)
                rows.append(dict(route=BROWSE, role=row['role'], channel=channel, query=url,
                                 browse_url=base, browse_page=page, browse_label=row['label'],
                                 covers_roles=list(row['covers']), company=None, exhausted=False))
    return rows


def browse_covered_pairs(city, roles, channels):
    """返回 (渠道, 岗位词) 中**实际生成了列表页任务**的组合。

    只有这里出现的组合才允许取消站内搜索回退；城市未适配、类目未命中、
    渠道未收录时都不算覆盖。
    """
    covered = set()
    for row in browse_rows(city, roles, channels):
        for role in row.get('covers_roles') or [row['role']]:
            covered.add((row['channel'], role))
    return covered


def search_channels(config):
    """除官网与人工导入以外、需要在计划里单独占预算的检索渠道。"""
    return [c for c in config.sources if c not in (OFFICIAL, MANUAL)]


def channel_query(channel, city, role, hiring_type):
    label, method = CHANNEL_METHODS.get(channel, (channel, '线上检索'))
    return f'{label} {method} {city} {role} {hiring_type}'.replace('  ', ' ').strip()


def fingerprint(config):
    from .spec import matching_config
    config=matching_config(config)
    data=config.model_dump(mode='json')
    if config.rules_version=='CN_MAINLAND-3.9':
        data.pop('rules_version',None);data.pop('candidate_education',None)
        if not data['budget']['per_city']:data['budget'].pop('per_city')
    value = {"config": data, "rules": config.rules_version}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def budget_profile(spec):
    """细粒度预算写进计划，而不是写进配置模型。

    配置模型会参与 `fingerprint()`：加一个字段 = 所有历史配置的指纹都变，等于静默
    改写历史任务的条件。因此列表页/详情/运行时长这类上限放在计划的 `budget_profile`，
    由 `start-run` 冻结进运行状态。
    """
    return {
        "per_city_channel": dict(getattr(spec.search_budget, 'per_city_channel', {}) or {}),
        "list_pages_per_city": spec.page_budget,
        "detail_reads_per_city": None,
        "runtime_minutes": spec.runtime_budget,
        "source": "effective_search_spec.page_budget/runtime_budget/per_city_channel",
    }


def cities(config):
    return list(dict.fromkeys(normalize_city(c) for c in config.target_cities))


def discovery_terms(config):
    return config.role_keywords if config.role_mode == "keywords" else config.company_scope_keywords


def build_plan(config, catalog=None, *, role_groups=None, company_assessments=None, knowledge=None, requested_companies=(), entry_knowledge=None):
    from .spec import spec_for_config
    spec=spec_for_config(config)
    config=spec.to_config()
    tasks, entry_tasks = [], []
    if role_groups is not None:
        role_groups = RoleExpansion.model_validate(role_groups).model_dump()
    synonym_expansions = {kw: synonyms_for(kw) for kw in
                          (config.role_keywords if config.role_mode == 'keywords' else [])
                          if synonyms_for(kw)}
    selected_by_city, selection = select_companies(config,catalog,role_groups,company_assessments,knowledge,requested_companies)
    other_channels = search_channels(config)
    official_enabled = OFFICIAL in config.sources
    for city in cities(config):
        selected = selected_by_city[city]
        local = []
        role_pairs = [(role, hiring_type) for role in discovery_terms(config)
                      for hiring_type in (recruitment_terms(config.recruitment_type) or [""])]
        # 1. 官网渠道的岗位查询词任务：每个岗位词一条，不含目录/名录类查询。
        #    官网不使用"监管企业名录"这类目录页：目录页不是岗位来源，也拿不到企业是否在招；
        #    企业名单一律从本渠道查询词结果里提取（official_channel.build_catalog）。
        #    未启用官网时不生成任何官网任务——用户只选平台渠道时不应凭空多出官网链路。
        if official_enabled:
            for role, hiring_type in role_pairs:
                local.append(dict(route="direct", role=role, channel=OFFICIAL,
                                  query=f'{city} {role} {hiring_type} 招聘 官网 工作地点'.replace("  ", " "),
                                  company=None, exhausted=False))
        # 2. 官网渠道的研究型检索任务：交给 domain_search，产出"正在招聘的企业"名单。
        #    与 direct 查询词任务并列，属于官网渠道自己的发现任务；平台渠道任务产出的
        #    企业仍然不进名单（见 official_channel._reject_reason）。
        if official_enabled:
            for role, hiring_type in role_pairs:
                local.append(dict(route=RESEARCH, role=role, channel=OFFICIAL,
                                  query=f'{city} {role} {hiring_type} 正在招聘的企业 名单 官网招聘'.replace("  ", " "),
                                  company=None, exhausted=False))
        # 3. 平台渠道：列表页直读是主路径，一页一条任务（便于按次对账）。
        local.extend(browse_rows(city, [role for role, _ in role_pairs], other_channels))
        # 3b. 只有"城市＋渠道＋岗位词"**实际生成了列表页任务**时才取消站内搜索回退；
        #     城市未适配、类目未命中都要退回站内搜索，否则该渠道会彻底缺失。
        covered = browse_covered_pairs(city, [role for role, _ in role_pairs], other_channels)
        for role, hiring_type in role_pairs:
            for channel in other_channels:
                if (channel, role) in covered:
                    continue
                local.append(dict(route="channel_search", role=role, channel=channel,
                                  query=channel_query(channel, city, role, hiring_type),
                                  company=None, exhausted=False))
        # 4. 企业任务（企业库/名单指定）：仍归官网渠道，是产出不是线索来源。
        # Never filter employers by headquarters/registration; outside employers can hire here.
        if official_enabled:
            for company in selected:
                name = company["legal_name"]
                reuse = entry_task(company,entry_knowledge if entry_knowledge is not None else knowledge,config,city,f'{city}-entry-{len(entry_tasks)+1}')
                if reuse:
                    entry_tasks.append(reuse)
                    continue
                term = " ".join(config.role_keywords) if config.role_mode == "keywords" else ""
                local.append(dict(route="enterprise", role=term, channel=OFFICIAL,
                                  query=f'{name} {city} {term} 招聘'.replace("  ", " "), company=name,
                                  exhausted=bool(company.get('company_budget_exhausted'))))
        # 预算：先按每渠道上限（官网优先分配时由 base_allocation 给出），再按每城总量。
        channel_caps = (spec.search_budget.per_city_channel or {}).get(city, {})
        city_cap = config.budget.city_searches(city)
        city_used, channel_used = 0, {}
        for index, row in enumerate(local):
            cap = channel_caps.get(row["channel"])
            room = city_used < city_cap and (cap is None or channel_used.get(row["channel"], 0) < cap)
            if room and not row["exhausted"]:
                city_used += 1
                channel_used[row["channel"]] = channel_used.get(row["channel"], 0) + 1
            employer = next((c for c in selected if c['legal_name'] == row["company"]), {})
            if row["company"]:
                reason = 'no_reusable_entry'
            elif row["route"] == RESEARCH:
                reason = 'official_research_query'
            elif row["route"] == BROWSE:
                reason = 'browse_list_first_page' if row.get("browse_page") == 1 else 'browse_list_paging'
            elif row["channel"] == OFFICIAL:
                reason = 'configured_discovery_query'
            else:
                reason = 'configured_channel_search'
            tasks.append(dict(task_id=f'{city}-{index+1}', target_city=city, role_keyword=row["role"],
                              query=row["query"], route=row["route"], channel=row["channel"],
                              stage=channel_stage(row["channel"]),
                              query_scope=row["role"] or ",".join(row.get("covers_roles") or []),
                              pagination_chain_id=row.get("browse_url") or f'{city}-{index+1}',
                              synonyms=synonym_expansions.get(row["role"], []),
                              **{k: row[k] for k in ("browse_url", "browse_page", "browse_label", "covers_roles") if k in row},
                              company=row["company"], company_source=employer.get('source_url'),
                              company_selection=employer.get('selection_decision'),
                              reason=reason,
                              status="planned" if room and not row["exhausted"] else "budget_exhausted",
                              executed_at=None, search_tool=None, candidate_count=0))
    channel_plan = {}
    for task in tasks:
        row = channel_plan.setdefault(task["channel"], {"routes": {}, "planned": 0, "budget_exhausted": 0,
                                                        "stage": task.get("stage")})
        row["routes"][task["route"]] = row["routes"].get(task["route"], 0) + 1
        if task["status"] in ("planned", "budget_exhausted"):
            row[task["status"]] += 1
    stage_order = [s for s in ("official", "recruitment", "campus", "bendibao_web", "public_accounts")
                   if any(t.get("stage") == s for t in tasks) or
                   (s == "official" and config.sources and OFFICIAL in config.sources)]
    return dict(plan_id=str(uuid.uuid4()), created_at=now(), config_fingerprint=fingerprint(config),
                protocol_version=PROTOCOL_VERSION, receipt_protocol=receipts.V2,
                channels=list(config.sources), stage_order=stage_order,
                effective_config=config.model_dump(mode="json"), effective_search_spec=spec.model_dump(mode='json'), rule_version=config.rules_version,
                budget_profile=budget_profile(spec),
                channel_access={c: {"mode": channel_registry.access_mode(c),
                                    "automation_allowed": channel_registry.automation_allowed(c),
                                    "login_required": channel_registry.requires_login(c),
                                    "browser_surface": channel_registry.browser_surface(c),
                                    "label": channel_registry.label(c)}
                                for c in config.sources},
                synonym_expansions=synonym_expansions, tasks=tasks, entry_tasks=entry_tasks, catalog_scope=(catalog or {}).get("scope"),
                role_groups=role_groups, company_assessments=company_assessments,
                requested_companies=list(requested_companies), company_selection=selection,
                channel_plan=channel_plan,
                browse_coverage={c: {"cities": sorted(BROWSE_CITIES), "categories": [x[2] for x in v["categories"]],
                                     "pages": v["pages"], "page_url": v["page_url"]}
                                 for c, v in BROWSE_CATALOG.items()},
                coverage="逐城市小预算候选发现，不代表城市或全国完整覆盖")


PLAN_GUARD_FIELDS = ("task_id", "query", "target_city", "role_keyword", "channel", "route", "stage", "company")
# 字段清单只有一份：receipts.RESULT_FIELDS。这里保留别名给既有调用方。
RECEIPT_RESULT_FIELDS = receipts.RESULT_FIELDS
RECEIPT_META_FIELDS = ("receipt_version", "receipt_id", "attempt_id", "identified",
                       "derived", "company_waivers", "exhausted")


def _city_search_limit(plan, config, city):
    """该校本轮可用搜索次数：分渠道配额时按各渠道配额之和，否则按城市额度。"""
    budget = ((plan or {}).get('effective_config') or {}).get('budget') or {}
    per_channel = budget.get('per_channel') or {}
    if per_channel:
        return sum(int(v) for v in per_channel.values())
    return config.budget.city_searches(city)


def validate_receipt(config, plan, data, run_protocol=None):
    if plan["config_fingerprint"] != fingerprint(config) or data.get("config_fingerprint") != fingerprint(config):
        raise ValueError("查询计划/候选与本次完整条件或规则版本不同，请重新生成并执行查询")
    if data.get("plan_id") != plan["plan_id"] or data.get("mode") not in ("agent_assisted", "codex_assisted"):
        raise ValueError("新检索必须提供本次计划关联的真实搜索记录；旧候选只能 refresh/process-candidates")
    version = receipts.resolve_version(data, run_protocol)
    planned = {t["task_id"]: t for t in plan["tasks"]}
    logs = data.get("search_log", [])
    if len({x["task_id"] for x in logs}) != len(logs):
        raise ValueError("查询任务记录重复")
    for item in logs:
        task = planned.get(item["task_id"])
        if not task or any(item.get(k) != task.get(k) for k in ["query", "target_city", "role_keyword"]):
            raise ValueError("实际查询与计划不一致")
        # 回执只能提交结果字段；任务归属、渠道、路由、阶段等计划字段不可被改写。
        for field in PLAN_GUARD_FIELDS:
            if field in item and item[field] != task.get(field):
                raise ValueError(f"回执不能覆盖计划字段 {field}：计划为 {task.get(field)!r}，回执为 {item[field]!r}")
        if item.get("status") not in ("success", "failed", "restricted", "not_run", "budget_exhausted"):
            raise ValueError("缺少明确查询执行状态")
        if version != receipts.V1:
            item.setdefault("receipt_version", version)
            if item["status"] == "success" and task.get("route") in receipts.EXHAUSTION_ROUTES \
                    and not isinstance(item.get("exhausted"), bool):
                raise ValueError(
                    f"回执 {item['task_id']} 缺少必填字段 exhausted（boolean）；"
                    "receipt-2 不会因缺字段降级为旧协议")
        if item["status"] in ("success", "failed", "restricted"):
            if task["status"] == "budget_exhausted" or not item.get("search_tool") or not item.get("executed_at"):
                raise ValueError("超预算查询或缺少执行时间/工具")
            if datetime.fromisoformat(item["executed_at"]) < datetime.fromisoformat(plan["created_at"]):
                raise ValueError("历史查询不能冒充本次新检索")
            if datetime.fromisoformat(item['executed_at']) > datetime.fromisoformat(now())+timedelta(minutes=5):
                raise ValueError('查询执行时间不能在未来')
    logged = {x["task_id"]: x for x in logs}
    for city in cities(config):
        used = plan.get('budget_used',{}).get('searches',{}).get(city,0)
        if used + sum(x.get('target_city') == city and x['status'] in ('success','failed','restricted') for x in logs) > _city_search_limit(plan, config, city):
            raise ValueError('后续搜索超过本轮剩余查询预算')
    for candidate in data.get("candidates", []):
        ids = candidate.get("task_ids", [])
        if not ids or any(i not in logged or logged[i]["status"] != "success" for i in ids):
            raise ValueError("候选缺少本次成功查询关联")
        expected = {planned[i]["target_city"] for i in ids}
        if set(candidate.get("target_cities", [])) != expected:
            raise ValueError("候选城市与查询归属不一致")
    for item in logs:
        count = sum(item["task_id"] in c.get("task_ids", []) for c in data.get("candidates", []))
        if item.get("candidate_count") != count:
            raise ValueError("查询候选数量与记录不一致")
    merged = []
    for task in plan["tasks"]:
        log = logged.get(task["task_id"], {})
        # 以计划为准：先取计划字段，再只叠加回执允许的结果字段。
        row = dict(task)
        row.update({k: log[k] for k in RECEIPT_RESULT_FIELDS if k in log})
        row["status"] = log.get("status", "not_run" if task["status"] == "planned" else task["status"])
        row["company"] = task.get("company")
        row["company_source"] = task.get("company_source")
        row["company_selection"] = task.get("company_selection")
        # 回执身份与新版字段必须原样带给控制器，否则批量入口会丢 exhausted/identified/derived。
        row["receipt_version"] = log.get("receipt_version") or version
        row["receipt_id"] = log.get("receipt_id") or (
            receipts.receipt_id({**log, "task_id": task["task_id"]}) if log else None)
        row["attempt_id"] = log.get("attempt_id") or (
            receipts.attempt_id({**log, "task_id": task["task_id"]}) if log else None)
        row["outcome"] = ("not_found_this_run"
                          if log.get("status") == "success" and not log.get("candidate_count")
                          else "see_execution_status")
        merged.append(row)
    return merged


def effective_config(config, source, overrides=None):
    from .spec import spec_for_config
    overrides = overrides or {}
    return {"values": config.model_dump(mode="json"), "effective_search_spec":spec_for_config(config).model_dump(mode='json'), "config_fingerprint": fingerprint(config),
            "origins": {k: ("本次CLI输入" if k in overrides else f"配置文件:{source}" if k in config.model_fields_set else "安全默认值") for k in config.model_fields},
            "note": "example/offline不是用户偏好；salary.currency业务默认不用于补全来源币种"}


def ensure_new_execution(plan, logs, output_root, state=None, run_id=None):
    """允许同一计划分多批提交。

    旧实现只要发现"该 plan_id 已导出过一次"，就拒绝该计划后续所有任务，于是
    "第一批提交任务 A、第二批提交任务 B"永远走不通，只能新建计划——而新建计划会
    把历史消耗清零，等于用换计划来重置预算。

    现在按 run_id + task_id + attempt_id/receipt_id 去重（`state["receipt_ledger"]`），
    只拒绝两种真正的问题：执行时间超出交接窗口、以及同一执行被当成两次新执行。
    """
    executed = [datetime.fromisoformat(x['executed_at']) for x in logs
                if x.get('executed_at') and x.get('status') in ('success', 'failed', 'restricted')]
    if executed and datetime.fromisoformat(now()) - max(executed) > timedelta(minutes=30):
        raise ValueError('查询记录超过30分钟交接窗口；请重新执行新计划或明确使用refresh')
    if state is not None:
        ledger = state.setdefault('receipt_ledger', {})
        rid = run_id or state.get('run_id')
        for row in logs:
            key = f"{rid}|{row.get('task_id')}|{row.get('attempt_id') or row.get('executed_at') or ''}|{row.get('receipt_id') or ''}"
            if key in ledger:
                raise ValueError(f"该执行已在本轮记录过（{row.get('task_id')} / {row.get('attempt_id')}）；"
                                 "重复提交不会重复计费，如只需补证据请用 record-action 修订")
        return
    # 没有运行状态时（历史入口）：只拦"同一计划、同一批任务"的重复导出，
    # 不再因为计划用过一次就拒绝全部后续任务。
    task_ids = tuple(sorted(str(x.get('task_id')) for x in logs))
    for path in Path(output_root).glob('*/results.json'):
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        discovery = previous.get('discovery', {})
        if discovery.get('plan_id') != plan['plan_id'] or discovery.get('operation') != 'new_search':
            continue
        previous_ids = tuple(sorted(str(x.get('task_id')) for x in discovery.get('search_log', [])))
        if task_ids and task_ids == previous_ids:
            raise ValueError('同一计划、同一批任务已提交过；第二批请提交此前未执行的任务')


def authorize_config(config, allow_example=False, offline=False):
    if config.config_kind=='offline' and not offline:
        raise ValueError('offline配置不能用于真实联网入口')
    if config.example_only:
        if not allow_example:
            raise ValueError("example_only 必须显式 --allow-example；演示不是用户条件")
    elif not offline and (config.config_kind != "user" or not config.user_input_confirmed):
        raise ValueError("请填写用户配置并明确 user_input_confirmed=true；不能靠删除 example_only 获得真实条件")


def extend_plan(config, plan, catalog, *, role_groups=None, company_assessments=None, knowledge=None, requested_companies=(), entry_knowledge=None):
    if plan['config_fingerprint'] != fingerprint(config):
        raise ValueError('不能扩展其他条件或规则版本的计划，请重新生成查询计划')
    if role_groups is not None and RoleExpansion.model_validate(role_groups).model_dump() != plan.get('role_groups'):
        raise ValueError('扩展不可改变岗位词组/性质，请生成新计划')
    role_groups = plan.get('role_groups')
    company_assessments = company_assessments if company_assessments is not None else plan.get('company_assessments')
    requested_companies = list(dict.fromkeys([*plan.get('requested_companies', []), *requested_companies]))
    previous_companies = [dict(company=name,target_city=city) for city,names in plan.get('budget_used',{}).get('companies',{}).items() for name in names]
    selected, selection = select_companies(config,catalog,role_groups,company_assessments,knowledge,requested_companies,[*plan['tasks'],*plan.get('entry_tasks',[]),*previous_companies])
    for city in cities(config):
        for company in selected[city]:
            local=[t for t in plan['tasks'] if t['target_city']==city]
            reuse = entry_task(company,entry_knowledge if entry_knowledge is not None else knowledge,config,city,f'{city}-entry-{len(plan.get("entry_tasks",[]))+1}')
            if reuse:
                plan.setdefault('entry_tasks',[]).append(reuse)
                continue
            term=" ".join(config.role_keywords) if config.role_mode=='keywords' else ''
            query=f"{company['legal_name']} {city} {term} 招聘".replace('  ',' ')
            if query in plan.get('query_history',[]) or any(t['query']==query for t in local):
                continue
            remaining = config.budget.city_searches(city)-plan.get('budget_used',{}).get('searches',{}).get(city,0)
            plan['tasks'].append(dict(task_id=f'{city}-{len(local)+1}',target_city=city,role_keyword=term,query=query,route='announcement_reverse' if company.get('discovery_route')=='announcement_reverse' else 'enterprise',channel='official_career',stage='official',derived_from='extend_plan',company=company['legal_name'],company_source=company.get('source_url'),company_selection=company['selection_decision'],reason='no_reusable_entry',created_at=now(),status='planned' if len(local)<remaining and not company.get('company_budget_exhausted') else 'budget_exhausted',executed_at=None,search_tool=None,candidate_count=0))
    plan.setdefault('company_selection_history', []).append(selection)
    plan['requested_companies'] = requested_companies
    plan['company_assessments'] = company_assessments
    plan['extended_at']=now()
    return plan


def followup_plan(config, requests, logs, parent=None, budget_used=None):
    """Executable successor, not search results. Carry the original run's spent budget."""
    parent = parent or {}
    used = budget_used or {'searches':{}, 'requests':{}, 'companies':{}}
    history = list(parent.get('query_history',[]))
    history.extend(x['query'] for x in logs if x.get('status') in ('success','failed','restricted'))
    tasks, skipped, seen = [], [], set(history)
    for request in requests:
        city, company = request['target_city'], request['company']
        role = ' '.join(config.role_keywords) if config.role_mode == 'keywords' else ''
        query = f'{company} {city} {role} 招聘'.replace('  ',' ')
        if query in seen:
            skipped.append(dict(query=query,target_city=city,company=company,status='not_run',reason='already_planned_or_searched_this_run'))
            continue
        seen.add(query)
        remaining = config.budget.city_searches(city) - used.get('searches',{}).get(city,0)
        allocated = sum(t['target_city']==city and t['status']=='planned' for t in tasks)
        allowance = config.budget.city_requests(cities(config).index(city),len(cities(config)),used.get('requests',{}).get(city,0))
        readable = allowance is None or allowance > 0
        tasks.append(dict(task_id=f'{city}-fallback-{len(tasks)+1}',target_city=city,company=company,
            role_keyword=role,query=query,route='enterprise_fallback',channel='official_career',stage='official',
            derived_from='followup_plan',company_source=request.get('source_url'),
            status='planned' if allocated < remaining and readable else 'budget_exhausted',
            reason=request['reason'] if readable else 'request_budget_exhausted',executed_at=None,search_tool=None,candidate_count=0))
    return dict(plan_id=str(uuid.uuid4()),parent_plan_id=parent.get('plan_id'),created_at=now(),
        protocol_version=PROTOCOL_VERSION,channels=list(config.sources),
        rule_version=config.rules_version,config_fingerprint=fingerprint(config),effective_config=config.model_dump(mode='json'),
        tasks=tasks,entry_tasks=[],skipped=skipped,budget_used=used,query_history=list(dict.fromkeys(history)),
        status='awaiting_assistant_search' if any(t['status']=='planned' for t in tasks) else 'no_executable_search',
        coverage='仅后续合并查询计划；尚未执行在线搜索，不表示企业无岗位')
