"""官网渠道：岗位查询词/研究型检索 -> 企业名单 -> 逐家企业官网检索。

本模块只处理官网渠道自己的线索。平台渠道（智联、猎聘、BOSS、前程无忧）发现的
企业不会进入本渠道名单：候选必须挂在官网渠道自己的查询任务上（task_ids 命中），
且链接不得落在招聘平台域名。两个渠道的计划互不引用。

例外只开在"本渠道自己的 research 任务"上：研究型检索（domain_search）的摘要
常指向平台页面，但企业名归属的是官网渠道的检索任务。这类企业以 lead_only=true
入名单，必须回官网核实后才能当证据用。

顺序语义：企业按其在查询结果里首次出现的顺序排列，逐家生成检索任务，
不重新排序、不按名称排序。
"""
from copy import deepcopy
from urllib.parse import urlsplit

from .geography import normalize_city
from .models import now
from .resources import name_key

# 招聘平台域名：这些域名下的页面即使出现企业名，也属于平台渠道线索，
# 不能回流到官网渠道名单。
PLATFORM_HOSTS = (
    "liepin.com", "zhipin.com", "zhaopin.com", "51job.com", "51jobcdn.com",
    "51jingying.com", "lagou.com", "kanzhun.com", "maimai.cn", "nowcoder.com",
    "yingjiesheng.com", "shixiseng.com", "dajie.com", "chinahr.com", "58.com",
    "ganji.com", "linkedin.com", "indeed.com", "glassdoor.com",
)

# 官网渠道的发现任务：岗位查询词检索（direct）与研究型企业名单检索（research）。
# 企业任务（route=enterprise）是产出，不是线索来源；平台渠道任务（channel_search /
# browse_list）无论产出什么企业名都不进本渠道名单。
DISCOVERY_ROUTES = ("direct", "research")
RESEARCH_ROUTE = "research"

# 研究型检索（domain_search）常把企业写成代称；这类名字不能进名单，否则后面
# 会生成一条"某上海世界500强公司 上海 招聘"的检索任务，跑出来全是噪音。
PLACEHOLDER_MARKERS = ("某", "猎头", "代招", "代发", "保密", "匿名", "一家", "多家")


def _host(url):
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_platform_url(url):
    host = _host(url)
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in PLATFORM_HOSTS)


def official_discovery_tasks(plan, city=None):
    """计划中属于官网渠道的查询词任务（不含企业任务、不含入口读取任务）。"""
    rows = []
    for task in plan.get("tasks", []):
        if task.get("channel", "official_career") != "official_career":
            continue
        if task.get("route") not in DISCOVERY_ROUTES:
            continue
        if city and task.get("target_city") != normalize_city(city):
            continue
        rows.append(task)
    return rows


def official_discovery_task_ids(plan, city=None):
    return {t["task_id"] for t in official_discovery_tasks(plan, city)}


def _reject_reason(candidate, discovery_ids, research_ids):
    tasks = set(candidate.get("task_ids") or [])
    if not tasks:
        return "missing_task_ids"
    if not tasks & discovery_ids:
        return "not_official_channel_task"
    research = bool(tasks & research_ids)
    name = (candidate.get("company") or "").strip()
    # 平台域名只有在本渠道自己的 research 任务里产出时才收：研究型检索的摘要
    # 往往指向平台页面，但企业归属的是官网渠道的检索任务，不是平台渠道任务。
    if is_platform_url(candidate.get("url")) and not research:
        return "platform_url"
    if not name:
        return "no_company_name"
    if any(marker in name for marker in PLACEHOLDER_MARKERS):
        return "placeholder_employer"
    if candidate.get("candidate_type") in ("enterprise_directory",):
        return "directory_page"
    return None


def build_catalog(plan, receipt, *, limit=None):
    """从官网渠道查询词的搜索结果里提取企业，形成独立名单。

    只读 receipt 的 candidates，不读任何其他渠道的产物。被剔除的候选逐条留痕，
    便于核对"为什么这家企业没进名单"。
    """
    discovery = official_discovery_tasks(plan)
    discovery_ids = {t["task_id"] for t in discovery}
    research_ids = {t["task_id"] for t in discovery if t.get("route") == RESEARCH_ROUTE}
    if not discovery_ids:
        raise ValueError("计划里没有官网渠道的岗位查询任务，无法生成企业名单")

    city_by_task = {t["task_id"]: t["target_city"] for t in discovery}
    companies, order, excluded = {}, [], []
    for index, candidate in enumerate(receipt.get("candidates", []), start=1):
        reason = _reject_reason(candidate, discovery_ids, research_ids)
        if reason:
            excluded.append({
                "company": candidate.get("company"),
                "title": candidate.get("title"),
                "url": candidate.get("url"),
                "reason": reason,
                "task_ids": candidate.get("task_ids") or [],
            })
            continue
        name = candidate["company"].strip()
        key = name_key(name)
        hit = set(candidate.get("task_ids") or []) & discovery_ids
        # lead_only 只给"证据落在平台页"的研究型线索；官网页面上的线索本身就是
        # 官方来源，不需要再标一次待核实。
        research = bool(hit & research_ids) and is_platform_url(candidate.get("url"))
        cities = sorted({city_by_task[t] for t in hit if t in city_by_task})
        if key in companies:
            row = companies[key]
            row["target_cities"] = sorted(set(row["target_cities"]) | set(cities))
            for field in ("official_url", "career_url"):
                row[field] = row.get(field) or candidate.get(field)
            row["appearances"] += 1
            row["discovery_task_ids"] = sorted(set(row["discovery_task_ids"]) | hit)
            if research and not row["lead_only"]:
                row["lead_only"] = True
                row["lead_source"] = "research_summary"
                row["verification_required"] = True
            continue
        if limit is not None and len(order) >= limit:
            excluded.append({
                "company": name, "title": candidate.get("title"), "url": candidate.get("url"),
                "reason": "over_limit", "task_ids": candidate.get("task_ids") or [],
            })
            continue
        order.append(key)
        companies[key] = {
            "legal_name": name,
            "official_url": candidate.get("official_url"),
            "career_url": candidate.get("career_url"),
            "source_url": candidate.get("url"),
            "version_date": now()[:10],
            "acquired_at": now(),
            "discovery_kind": "unknown",
            "listed_status": None,
            "soe_status": None,
            "notes": (f"官网渠道研究型检索「{candidate.get('query')}」第 {index} 条线索；"
                      "企业名待官网核实" if research else
                      f"官网渠道查询词「{candidate.get('query')}」搜索结果第 {index} 条；仅本渠道线索"),
            "target_cities": cities,
            "discovery_order": len(order),
            "appearances": 1,
            "source_type": candidate.get("source_type"),
            "lead_only": research,
            "lead_source": "research_summary" if research else candidate.get("source_type"),
            "verification_required": research,
            "discovery_task_ids": sorted(hit),
        }

    rows = [companies[key] for key in order]
    return {
        "schema_version": "query-discovery-catalog-1",
        "source_kind": "query_discovery",
        "scope": "仅官网渠道岗位查询词/研究型检索结果中出现的企业；不含任何平台渠道任务发现的企业",
        "source_url": f"plan:{plan.get('plan_id')}",
        "version_date": now()[:10],
        "acquired_at": now(),
        "current_status_verified": False,
        "complete_national_list": False,
        "companies": rows,
        "search_tasks": [{"task_id": t["task_id"], "target_city": t["target_city"],
                          "query": t["query"], "route": t.get("route")} for t in discovery],
        "excluded": excluded,
        "known_limitations": [
            "名单只来自本渠道查询词的公开搜索结果，不是完整企业库；未出现不代表该企业不招人",
            "平台渠道（智联/猎聘/BOSS/前程无忧）任务发现的企业不会回流到本名单",
            "lead_only=true 的企业来自研究型检索摘要，只是线索：企业全称、是否在招、岗位是否在目标城市都须在官网原文核实",
            "placeholder_employer 是「某公司/猎头代招」这类代称，无法生成可执行的官网检索任务",
        ],
    }


def plan_official_tasks(config, plan, catalog, *, city=None):
    """按名单顺序为每家企业生成官网检索任务，并遵守每城预算。

    顺序即发现顺序；预算不足的企业标 budget_exhausted，不删行、不静默丢弃。
    """
    rows = catalog.get("companies") or []
    if not rows:
        raise ValueError("企业名单为空，无法生成官网检索任务")
    result = deepcopy(plan)
    used = result.get("budget_used", {}).get("searches", {})
    term = " ".join(config.role_keywords) if config.role_mode == "keywords" else ""
    target_cities = [normalize_city(c) for c in config.target_cities]
    if city:
        target_cities = [normalize_city(city)]

    added, skipped = [], []
    caps_by_city = ((plan.get("effective_search_spec") or {}).get("search_budget") or {}).get("per_city_channel") or {}
    for target_city in target_cities:
        local = [t for t in result["tasks"] if t["target_city"] == target_city]
        total = config.budget.city_searches(target_city)
        # 官网优先分配时官网有单独的渠道额度，不能被企业任务突破。
        official_cap = caps_by_city.get(target_city, {}).get("official_career") or total
        official_planned = sum(1 for t in local if t.get("channel", "official_career") == "official_career")
        remaining = min(total, official_cap) - used.get(target_city, 0) - official_planned
        planned = {name_key(t["company"]) for t in local if t.get("company")}
        seen_queries = {t["query"] for t in local}
        for row in rows:
            name = row["legal_name"]
            key = name_key(name)
            row_cities = [normalize_city(c) for c in (row.get("target_cities") or target_cities)]
            if target_city not in row_cities:
                skipped.append({"company": name, "city": target_city, "reason": "discovered_in_other_city"})
                continue
            if key in planned:
                skipped.append({"company": name, "city": target_city, "reason": "already_in_plan"})
                continue
            query = f"{name} {target_city} {term} 招聘".replace("  ", " ").strip()
            if query in seen_queries:
                skipped.append({"company": name, "city": target_city, "reason": "duplicate_query"})
                continue
            status = "planned" if remaining > 0 else "budget_exhausted"
            if remaining > 0:
                remaining -= 1
            task = {
                "task_id": f"{target_city}-{len(local) + 1}",
                "target_city": target_city,
                "role_keyword": term,
                "query": query,
                "route": "enterprise",
                "channel": "official_career",
                "company": name,
                "company_source": row.get("source_url"),
                "company_selection": {"selection": "selected", "source": "official_channel_query_discovery",
                                      "discovery_order": row.get("discovery_order")},
                "reason": "official_channel_company_search",
                "order": row.get("discovery_order"),
                "status": status,
                "executed_at": None,
                "search_tool": None,
                "candidate_count": 0,
            }
            result["tasks"].append(task)
            local.append(task)
            planned.add(key)
            seen_queries.add(query)
            added.append(task)

    result["official_channel"] = {
        "source": "query_discovery",
        "catalog_scope": catalog.get("scope"),
        "catalog_companies": len(rows),
        "lead_only_companies": sorted(r["legal_name"] for r in rows if r.get("lead_only")),
        "added_tasks": len(added),
        "planned": sum(1 for t in added if t["status"] == "planned"),
        "budget_exhausted": [t["company"] for t in added if t["status"] == "budget_exhausted"],
        "skipped": skipped,
        "independence": "名单仅来自本渠道查询词/研究型检索结果；平台渠道任务产出的企业不回填",
        "verification_note": "lead_only 企业来自检索摘要，须先在官网核实企业全称与在招状态",
        "extended_at": now(),
    }
    return result
