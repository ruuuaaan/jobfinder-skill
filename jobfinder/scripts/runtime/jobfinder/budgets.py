"""统一预算账本：规划、派发、回执、恢复、阶段收尾共用一个口径。

历史问题
--------
1. `start_run` 冻结预算时漏了 `budget.per_city`，导致"某城 5 次、其它城 40 次"的
   配置在运行期被忽略，一律按 `searches_per_city` 算。
2. 任务派发按渠道配额判断，官网核实任务收尾却按 `by_stage` 另一本账判断 —— 于是
   出现"派发说还有额度、收尾说已用尽"，或反过来。
3. 分批执行/失败重试/恢复时，同一笔消耗可能被重复扣或被重置。

本模块把"一笔消耗"定义清楚：
- 每执行一次任务，记 1 次 `search`（渠道账），并按任务路由再记一次计量类型：
  `list_page`（列表页/翻页）、`detail_read`（岗位详情/官网核实/入口核验）。
- 渠道配额（`per_channel` / `per_city_channel` / `per_city` / `searches_per_city`）
  只约束 `search`；计量类型各有自己的上限（`list_pages_per_city` /
  `detail_reads_per_city`），因此"一条查询"不能绕过详情读取预算。
- HTTP 请求量由网络层独立统计（`http_requests`），不与搜索次数混算。
- 累计运行时间单独记，不折算成搜索次数。

`by_stage` 只用于报告，不再作为第二道门。
"""
from __future__ import annotations

from datetime import datetime, timedelta

SEARCH = "search"
LIST_PAGE = "list_page"
DETAIL_READ = "detail_read"
KINDS = (SEARCH, LIST_PAGE, DETAIL_READ)

# 路由 → 计量类型。一条任务永远记 1 次 search，另外按这张表再记一次。
ROUTE_KIND = {
    "browse_list": LIST_PAGE,
    "channel_search": SEARCH,
    "direct": SEARCH,
    "research": SEARCH,
    "enterprise": DETAIL_READ,
    "enterprise_fallback": DETAIL_READ,
    "entry_reuse": DETAIL_READ,
    "official_verify": DETAIL_READ,
    "announcement_reverse": DETAIL_READ,
}


def kind_for(route: str | None) -> str:
    return ROUTE_KIND.get(route or "", SEARCH)


def freeze(plan: dict, profile: dict | None = None) -> dict:
    """把计划里的预算冻进运行状态；缺失即 None（表示该维度不限）。

    `profile` 是计划顶层的可选 `budget_profile`：列表页/详情/运行时长这类
    细粒度上限放在这里，而不是塞进 `SearchConfig`——改配置模型会让历史配置的
    指纹全部变化，等于静默改写历史任务的条件。
    """
    plan_budget = ((plan or {}).get("effective_config") or {}).get("budget") or {}
    profile = profile or {}
    return {
        "searches_per_city": plan_budget.get("searches_per_city"),
        "per_city": dict(plan_budget.get("per_city") or {}),
        "per_channel": dict(plan_budget.get("per_channel") or {}),
        "per_city_channel": dict(profile.get("per_city_channel") or {}),
        "companies_per_city": plan_budget.get("companies_per_city"),
        "http_requests": plan_budget.get("requests"),
        "list_pages_per_city": profile.get("list_pages_per_city"),
        "detail_reads_per_city": profile.get("detail_reads_per_city"),
        "runtime_minutes": profile.get("runtime_minutes"),
        "allocation_policy": plan_budget.get("allocation_policy") or "shared",
        "source": "plan.effective_config.budget + plan.budget_profile",
    }


def cap(budget: dict, channel: str | None, city: str | None) -> int | None:
    """某渠道在某城的搜索配额。优先级：城市×渠道 → 渠道 → 城市 → 全局默认。

    没配 `per_channel` 时，各渠道共用城市额度（旧共享模式），因此这里返回城市额度；
    调用方据此判断"本次该渠道还能不能搜"，与收尾判断用的是同一个函数。
    """
    budget = budget or {}
    per_city_channel = budget.get("per_city_channel") or {}
    city_row = per_city_channel.get(city) or {}
    if channel in city_row:
        return int(city_row[channel])
    per_channel = budget.get("per_channel") or {}
    if channel in per_channel:
        return int(per_channel[channel])
    per_city = budget.get("per_city") or {}
    if city in per_city:
        return int(per_city[city])
    value = budget.get("searches_per_city")
    return None if value is None else int(value)


def kind_cap(budget: dict, kind: str, city: str | None) -> int | None:
    budget = budget or {}
    key = {LIST_PAGE: "list_pages_per_city", DETAIL_READ: "detail_reads_per_city"}.get(kind)
    if not key:
        return None
    value = budget.get(key)
    return None if value is None else int(value)


def _bucket(state: dict, *path) -> dict:
    node = state.setdefault("budget_used", {})
    if not isinstance(node, dict):
        node = {}
        state["budget_used"] = node
    for key in path:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    return node


def used(state: dict, channel: str | None, city: str | None, kind: str = SEARCH) -> int:
    """已用量。kind=search 走渠道账；其它计量类型走 by_kind。"""
    budget_used = state.get("budget_used") or {}
    if kind == SEARCH:
        row = (budget_used.get("by_channel") or {}).get(channel or "") or {}
        return int(row.get(city, 0))
    row = ((budget_used.get("by_kind") or {}).get(kind) or {}).get(channel or "") or {}
    return int(row.get(city, 0))


def remaining(state: dict, channel: str | None, city: str | None, kind: str = SEARCH) -> int | None:
    limit = cap(state.get("scope", {}).get("budget") or {}, channel, city) if kind == SEARCH \
        else kind_cap(state.get("scope", {}).get("budget") or {}, kind, city)
    if limit is None:
        return None
    return max(0, limit - used(state, channel, city, kind))


def channel_remaining(state: dict, channel: str, city: str | None) -> int | None:
    return remaining(state, channel, city, SEARCH)


def kind_remaining(state: dict, kind: str, city: str | None, channel: str | None = None) -> int | None:
    """计量类型的剩余：配置上限 - 该城全部渠道累计（或指定渠道累计）。"""
    limit = kind_cap(state.get("scope", {}).get("budget") or {}, kind, city)
    if limit is None:
        return None
    if channel is None:
        row = (state.get("budget_used") or {}).get("by_kind", {}).get(kind, {}) or {}
        spent = sum(int(v.get(city, 0)) for v in row.values())
    else:
        spent = used(state, channel, city, kind)
    return max(0, limit - spent)


def total_remaining(state: dict, city: str | None) -> int | None:
    """该城还能执行多少次任务；None 表示未设上限。

    配了 per_channel 时是各渠道剩余之和（渠道各花各的）；否则是城市单池剩余。
    """
    budget = state.get("scope", {}).get("budget") or {}
    per_channel = budget.get("per_channel") or {}
    if per_channel:
        channels = [c for c in (state.get("scope", {}).get("channels") or per_channel)]
        total = 0
        for channel in channels:
            limit = cap(budget, channel, city)
            if limit is None:
                continue
            total += max(0, limit - used(state, channel, city))
        return total
    limit = cap(budget, None, city)
    if limit is None:
        return None
    spent = ((state.get("budget_used") or {}).get("searches") or {}).get(city, 0)
    return max(0, int(limit) - int(spent))


def stage_remaining(state: dict, stage: str, city: str | None) -> int | None:
    """阶段口径的剩余额度，**仅用于报告**。

    历史实现用它给官网核实任务做第二道门，与派发口径不一致；现在统一由
    `channel_remaining` 判断，这个函数只给 finalize/状态表展示用。
    """
    channels = [c for c in (state.get("scope", {}).get("channels") or [])
                if _stage(c) == stage]
    values = [channel_remaining(state, c, city) for c in channels]
    known = [v for v in values if v is not None]
    if not known:
        return None
    return sum(known)


def _stage(channel: str) -> str:
    from .channels import stage_of
    return stage_of(channel)


def charge(state: dict, task: dict, kind: str | None = None, *, include_search: bool = True) -> dict:
    """记一笔真实执行消耗。重复回执/证据修订不走这里。

    `include_search`：任务级执行同时占 1 次渠道搜索 + 1 次计量类型；
    独立的详情读取（例如浏览器辅助检索打开岗位页）只记 `detail_read`，
    不再额外占用渠道搜索配额，否则一次采样会被算成两次搜索。
    """
    city = task.get("target_city")
    channel = task.get("channel")
    stage = task.get("stage")
    kind = kind or kind_for(task.get("route"))
    if include_search:
        searches = _bucket(state, "searches")
        searches[city] = int(searches.get(city, 0)) + 1
        by_stage = _bucket(state, "by_stage", stage)
        by_stage[city] = int(by_stage.get(city, 0)) + 1
        by_channel = _bucket(state, "by_channel", channel)
        by_channel[city] = int(by_channel.get(city, 0)) + 1
    by_kind = _bucket(state, "by_kind", kind, channel)
    by_kind[city] = int(by_kind.get(city, 0)) + 1
    return {"kind": kind, "channel": channel, "city": city}


def note_http(state: dict, city: str | None, count: int = 1) -> None:
    requests = _bucket(state, "http_requests")
    requests[city] = int(requests.get(city, 0)) + int(count)


def snapshot(state: dict) -> dict:
    """预算面板：逐渠道/逐城市，供 finalize 与导出展示。"""
    scope = state.get("scope", {})
    budget = scope.get("budget") or {}
    rows = []
    for city in scope.get("cities") or [None]:
        for channel in scope.get("channels") or []:
            limit = cap(budget, channel, city)
            spent = used(state, channel, city)
            rows.append({
                "city": city, "channel": channel, "search_cap": limit, "search_used": spent,
                "search_remaining": None if limit is None else max(0, limit - spent),
                "list_page_used": used(state, channel, city, LIST_PAGE),
                "detail_read_used": used(state, channel, city, DETAIL_READ),
            })
    return {"rows": rows, "budget": budget,
            "total_remaining": {city: total_remaining(state, city) for city in scope.get("cities") or []},
            "runtime_seconds": runtime_seconds(state),
            "http_requests": dict((state.get("budget_used") or {}).get("http_requests") or {})}


def runtime_seconds(state: dict, ended_at: str | None = None) -> int | None:
    started = state.get("started_at")
    if not started:
        return None
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(ended_at) if ended_at else datetime.now(start.tzinfo)
    except (TypeError, ValueError):
        return None
    return max(0, int((end - start).total_seconds()))


def runtime_remaining(state: dict) -> int | None:
    budget = state.get("scope", {}).get("budget") or {}
    minutes = budget.get("runtime_minutes")
    if minutes is None:
        return None
    spent = runtime_seconds(state) or 0
    return max(0, int(minutes) - spent // 60)


def exhausted_note(state: dict, task: dict) -> str | None:
    """预算耗尽时的说明文本：明确"配额用尽"而不是"网站没有更多结果"。"""
    from .channels import label
    if task.get("route") == "official_verify":
        return "官网渠道搜索配额已用尽，该企业未完成核实（覆盖缺口，不代表该企业无岗位）"
    if task.get("route") == "browse_list":
        return (f"{label(task.get('channel'))}列表页预算已用尽，翻页未到底"
                "（覆盖缺口，不代表该网站没有更多岗位）")
    return (f"{label(task.get('channel'))}本轮渠道配额已用尽，该任务未执行"
            "（覆盖缺口，不代表该网站没有更多岗位）")


def deadline_from(seconds: int) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=int(seconds))).isoformat(timespec="seconds")
