"""统一的运行控制器：阶段顺序、平台登录前置、分批回执与整轮完成判定。

设计边界（必须先读）
--------------------
本模块只约束**经过本项目入口**的动作：`start-run / next-action / record-action /
close-stage / finalize`，以及 `new-search`、`process-browser`、`resume-traversal`
等会读取运行状态的命令。宿主若不调用这些命令、而是自己直接写 Excel 或直接调用
匹配/导出函数，控制器无法阻止它；那种情况下产物里没有 `run_id` 与完成度区块，
应被视为未经控制器验证的结果，而不是"已完成检索"。

完成度粒度（workflow-1.2 起）
----------------------------
"渠道跑到尾"不是一个布尔值，而是**若干独立搜索范围**各自的结论。一个范围由
`channel + city + query_scope + filter_fingerprint + pagination_chain_id` 组成：
北京到底不代表深圳到底，岗位词 A 到底不代表 B 到底，分页链末页只关闭该链。
`COMPLETE` 只表示"本轮声明范围完成"，从不表示"该网站全部岗位已查尽"。

数据只写入 `<workspace>/data/runs/<run_id>/`，不写技能安装目录。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import budgets, receipts
from . import channels as channel_registry
from .models import now, SHANGHAI

PROTOCOL_VERSION = "workflow-1.2"
STATE_SCHEMA = "workflow-run-2"

STAGES = channel_registry.STAGE_ORDER
STAGE_LABELS = channel_registry.STAGE_LABELS
CHANNEL_STAGE = {c.name: c.stage for c in channel_registry.CHANNELS if c.stage != "none"}
PLATFORM_CHANNELS = channel_registry.PLATFORM
NON_STAGE_CHANNELS = channel_registry.NON_STAGE

EXHAUSTION_ROUTES = ("direct", "research", "browse_list", "channel_search")
DISCOVERY_ROUTES = ("direct", "research")
VERIFY_ROUTE = "official_verify"
ENTRY_ROUTES = ("enterprise", "entry_reuse", "announcement_reverse", "enterprise_fallback")

CHANNEL_INFO = {c.name: {"label": c.label, "stage": c.stage, "note": c.note}
                for c in channel_registry.CHANNELS if c.supported}
SUPPORTED_CHANNELS = channel_registry.SUPPORTED

TASK_TERMINAL = ("executed", "blocked", "skipped_by_user", "budget_exhausted")
MAX_TASK_ATTEMPTS = 2

VERDICTS = ("COMPLETE", "PARTIAL", "WAITING_USER", "IN_PROGRESS", "FAILED")


# ---------------------------------------------------------------- 基础工具

def _now_dt():
    return datetime.now(SHANGHAI)


def parse_time(value):
    return datetime.fromisoformat(value)


def channel_stage(channel):
    """未知渠道按招聘平台处理：宁可要求登录，也不让它悄悄跳过前置。"""
    return CHANNEL_STAGE.get(channel, "recruitment")


def stage_order(channels_, *, user_selected=False):
    """本轮实际阶段顺序；只按所选渠道裁剪，不强行补回未选渠道。"""
    present = {channel_stage(c) for c in channels_ if c not in NON_STAGE_CHANNELS}
    order = [s for s in STAGES if s in present]
    if not order:
        order = ["official"]
    return order


def run_root(workspace):
    return Path(workspace) / "data" / "runs"


def state_file(workspace, run_id):
    return run_root(workspace) / run_id / "state.json"


def channel_menu(config=None):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "stage_order": list(STAGES),
        "menu": channel_registry.menu(config),
        "config_channels": list(getattr(config, "sources", []) or []) if config is not None else [],
        "guidance": "必须先让用户勾选渠道，再把选中的 --channel 传给 start-run；"
                    "不要依据配置文件静默决定范围。用户全选时可用 --all-channels。",
    }


def resolve_channel_selection(channels_, all_channels=False):
    return channel_registry.resolve_selection(channels_, all_channels)


# ---------------------------------------------------------------- 完成度范围

def _norm_scope(value) -> str:
    import re
    text = re.sub(r"\s+", "", str(value or "")).casefold()
    return text


def filter_fingerprint(task) -> str:
    """查询条件指纹：同范围内的翻页继承同一指纹，不同筛选/口径自然分开。"""
    payload = {
        "route": task.get("route"),
        "browse_url": task.get("browse_url"),
        "covers": sorted(task.get("covers_roles") or []),
        "access_kind": budgets.kind_for(task.get("route")),
        "channel": task.get("channel"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:16]


def scope_of(task) -> dict:
    query_scope = task.get("query_scope")
    if query_scope is None:
        query_scope = task.get("role_keyword") or ",".join(task.get("covers_roles") or [])
    chain = task.get("pagination_chain_id")
    if chain is None:
        chain = task.get("browse_url") or task.get("task_id")
    return {
        "channel": task.get("channel"),
        "city": task.get("target_city"),
        "query_scope": _norm_scope(query_scope),
        "filter_fingerprint": task.get("filter_fingerprint") or filter_fingerprint(task),
        "pagination_chain_id": chain or task.get("task_id"),
    }


def scope_key(task) -> str:
    scope = scope_of(task)
    return "|".join(str(scope[k]) for k in ("channel", "city", "query_scope",
                                            "filter_fingerprint", "pagination_chain_id"))


def _scopes(state, stage=None) -> dict:
    out: dict[str, dict] = {}
    for task in state["tasks"]:
        if stage and task.get("stage") != stage:
            continue
        if task.get("skipped_stage") or task.get("stage") == "skipped":
            continue
        key = scope_key(task)
        row = out.setdefault(key, {"key": key, **scope_of(task), "tasks": [],
                                   "exhausted": False, "executed": 0, "gap": None})
        row["tasks"].append(task)
        if task.get("exhausted") is True:
            row["exhausted"] = True
        if task.get("status") in ("executed", "budget_exhausted"):
            row["executed"] += 1
    return out


def open_scopes(state, stage=None) -> list[dict]:
    """已执行但没声明到底、预算仍有剩余、且没有后续任务承接的范围。"""
    out = []
    for key, row in _scopes(state, stage).items():
        if row["exhausted"] or not row["executed"]:
            continue
        live = [t for t in row["tasks"] if t.get("status") not in TASK_TERMINAL]
        if live:
            continue
        declared_false = [t for t in row["tasks"]
                          if t.get("status") == "executed" and t.get("exhausted") is False]
        continued = [t for t in row["tasks"] if t.get("continued")]
        if not declared_false or continued:
            continue
        remaining = task_budget_remaining(state, declared_false[0])
        if remaining == 0:
            row["gap"] = "budget_exhausted"
            continue
        out.append(row)
    return out


def exhausted_scope_keys(state, stage=None) -> set:
    return {k for k, row in _scopes(state, stage).items() if row["exhausted"]}


def scope_completion(state, stage=None) -> list[dict]:
    rows = []
    for key, row in _scopes(state, stage).items():
        remaining = None
        first = row["tasks"][0] if row["tasks"] else None
        if first:
            remaining = task_budget_remaining(state, first)
        rows.append({"key": key, "channel": row["channel"], "city": row["city"],
                     "query_scope": row["query_scope"],
                     "pagination_chain_id": row["pagination_chain_id"],
                     "exhausted": row["exhausted"], "executed": row["executed"],
                     "budget_remaining": remaining,
                     "status": ("exhausted" if row["exhausted"] else
                                "budget_limited" if remaining == 0 else
                                "open" if remaining is None or remaining > 0 else "unknown")})
    return rows


# ---------------------------------------------------------------- 创建运行

def start_run(config, plan, workspace, *, channels=None, public_only=False,
              auth_policy=None, run_id=None, budget_used=None, previous_run=None,
              user_confirmed_scope=False, browser_surface=None):
    """冻结本轮执行范围并落盘。

    `user_confirmed_scope` 只表示**用户明确只要当前渠道范围**；助手自行少选渠道时
    不得传 True，否则完成度会把"少跑了"记成"用户要求的范围"。
    `browser_surface`：`external`（用户本机独立浏览器，平台渠道默认）/ `embedded`（显式选择）。
    """
    scope = list(dict.fromkeys(channels or plan.get("channels")
                               or plan.get("effective_config", {}).get("sources", [])))
    unknown = [c for c in scope if c not in NON_STAGE_CHANNELS and c not in CHANNEL_STAGE]
    if unknown:
        raise ValueError("未建模的渠道：" + "、".join(unknown) +
                         "；请在 channel_registry.py 中显式建模后再启用")
    if public_only:
        policy = "public_only"
    else:
        policy = auth_policy or "required"
    if policy not in ("required", "public_only"):
        raise ValueError("platform_auth_policy 只能是 required 或 public_only")
    needs_external = any(channel_registry.browser_surface(c) == "external" for c in scope)
    surface = browser_surface or ("external" if needs_external and policy == "required" else "embedded")
    if surface not in ("external", "embedded"):
        raise ValueError("browser_surface 只能是 external 或 embedded")

    plan_tasks = [_task_from_plan(t, scope) for t in plan.get("tasks", [])]
    plan_tasks = [t for t in plan_tasks if t["stage"] != "skipped"]
    entry_tasks = [_task_from_plan(t, scope, derived_from="plan.entry_tasks")
                   for t in plan.get("entry_tasks", [])]
    entry_tasks = [t for t in entry_tasks if t["stage"] != "skipped"]

    run_id = run_id or "run-" + uuid.uuid4().hex[:12]
    order = stage_order(scope)
    state = {
        "schema_version": STATE_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "receipt_protocol": plan.get("receipt_protocol") or receipts.V2,
        "run_id": run_id,
        "created_at": now(),
        "started_at": now(),
        "updated_at": now(),
        "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("schema_version") or plan.get("plan_version") or "plan-1",
        "config_fingerprint": plan.get("config_fingerprint"),
        "rule_version": plan.get("rule_version"),
        "scope": {
            "cities": list(plan.get("effective_config", {}).get("target_cities") or []),
            "roles": list(plan.get("effective_config", {}).get("role_keywords") or []),
            "channels": scope,
            "stage_order": order,
            "platform_auth_policy": policy,
            "browser_surface": surface,
            "user_scope_explicit": bool(channels),
        },
        "current_stage": order[0],
        "stages": {s: {"status": "pending", "closed_at": None, "reason": None} for s in order},
        "tasks": plan_tasks + entry_tasks,
        "auth": {},
        "budget_used": budget_used or {},
        "events": [],
        "derived_tasks": [],
        "scope_adjustments": [],
        "previous_run": previous_run,
        "platform_auth_required": policy == "required",
    }
    # 预算上限冻结进运行状态：派发、回执、恢复、收尾都只认这里的值。
    state["scope"]["budget"] = budgets.freeze(plan, plan.get("budget_profile"))
    # 范围覆盖度：只覆盖部分阶段时，必须区分"用户明确只要这些"与"助手自行收窄"。
    covered = set(order)
    missing = [s for s in STAGES if s not in covered]
    state["scope"]["covered_stages"] = sorted(covered)
    state["scope"]["missing_stages"] = missing
    state["scope"]["user_scope_confirmed"] = bool(user_confirmed_scope and channels)
    state["scope"]["narrowed_by"] = ("user" if state["scope"]["user_scope_confirmed"]
                                     else "agent" if missing else "none")
    if missing:
        state["scope"]["coverage"] = "partial_scope"
        state["scope_adjustments"].append({
            "kind": "narrowed_scope", "missing_stages": missing,
            "user_confirmed": state["scope"]["user_scope_confirmed"],
            "narrowed_by": state["scope"]["narrowed_by"], "at": now()})
    else:
        state["scope"]["coverage"] = "full_scope"
    # 平台访问状态（冷却/受限）来自磁盘，跨 run 保留；不因新建 run 自动清除。
    try:
        from . import access_control
        state["platform_state"] = {
            c: view for c in scope
            if (view := access_control.cooldown(workspace, c))
        }
    except Exception:
        state["platform_state"] = {}
    # 浏览器能力来自实际探测，不接受模型自述。
    state["browser"] = _detect_browser(workspace)
    state["access"] = {"receipt_protocol": state["receipt_protocol"],
                       "browser_surface": surface,
                       "linkedin_mode": channel_registry.access_mode("linkedin")}
    _append_event(state, {"kind": "start_run", "plan_id": state["plan_id"],
                          "channels": scope, "stage_order": order, "auth_policy": policy,
                          "browser_surface": surface,
                          "user_scope_confirmed": state["scope"]["user_scope_confirmed"],
                          "narrowed_by": state["scope"]["narrowed_by"]})
    save_state(state, workspace)
    return state


def refresh_browser_state(state, workspace) -> dict:
    """用实际探测刷新浏览器会话信息。

    只读探测（service.json + 端口 + pid），不会启动浏览器。用于 next-action 之类
    的"下一步判断"：浏览器被关掉后应立刻显示需要重新打开，而不是继续显示旧会话。
    """
    detected = _detect_browser(workspace)
    state["browser"] = detected
    for channel in state.get("scope", {}).get("channels", []):
        record = (state.get("auth") or {}).get(channel) or {}
        if not record.get("backend_session_id"):
            continue
        if detected.get("status") == "unavailable":
            record["status"] = "blocked"
            record["reason"] = detected.get("reason")
        elif detected.get("session_id") and record.get("backend_session_id") \
                and detected["session_id"] != record["backend_session_id"]:
            record["status"] = "invalidated"
            record["reason"] = "浏览器会话已重建（进程或配置目录变化），需要重新核验登录"
    return detected


def _detect_browser(workspace) -> dict:
    try:
        from .browser_backend import capability_report
        return capability_report(workspace)
    except Exception as exc:  # 探测失败不能变成"能力可用"
        return {"backend": "external_browser", "status": "unsupported",
                "installed": [], "capabilities": {}, "reason": f"浏览器能力探测失败：{exc}",
                "requires_local_connector": True, "detected_by": "error"}


def _task_from_plan(task, scope, derived_from=None):
    channel = task.get("channel") or "official_career"
    stage = task.get("stage") or channel_stage(channel)
    included = channel in scope or channel in NON_STAGE_CHANNELS
    row = {
        "task_id": task["task_id"],
        "target_city": task.get("target_city"),
        "role_keyword": task.get("role_keyword"),
        "query": task.get("query"),
        "route": task.get("route"),
        "channel": channel,
        "stage": stage,
        "company": task.get("company"),
        "planned_status": task.get("status", "planned"),
        "status": "pending" if task.get("status", "planned") == "planned" else "budget_exhausted",
        "attempts": 0,
        "executed_at": None,
        "search_tool": None,
        "candidate_count": 0,
        "evidence_ref": None,
        "reason": None,
        "derived_from": derived_from or task.get("derived_from"),
        "browse_page": task.get("browse_page"),
        "browse_url": task.get("browse_url"),
        "covers_roles": task.get("covers_roles"),
        "skipped_stage": not included,
        "exhausted": None,
    }
    row["query_scope"] = task.get("query_scope") or task.get("role_keyword") \
        or ",".join(task.get("covers_roles") or [])
    row["filter_fingerprint"] = task.get("filter_fingerprint") or filter_fingerprint(row)
    row["pagination_chain_id"] = (task.get("pagination_chain_id") or task.get("browse_url")
                                  or task["task_id"])
    if not included:
        row["stage"] = "skipped"
    return row


# ---------------------------------------------------------------- 持久化

def save_state(state, workspace):
    path = state_file(workspace, state["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    events = state.get("events", [])
    if events:
        (path.parent / "events.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    return path


def load_state(workspace, run_id):
    path = state_file(workspace, run_id)
    if not path.exists():
        raise ValueError(f"运行 {run_id} 不存在；请先 start-run")
    return upgrade_state(json.loads(path.read_text(encoding="utf-8")), workspace)


def upgrade_state(state, workspace=None):
    """旧运行记录只补齐缺的字段，不改写历史条件、证据时间与配置指纹。"""
    version = state.get("schema_version")
    if version == STATE_SCHEMA:
        return state
    state.setdefault("schema_version", STATE_SCHEMA)
    state.setdefault("created_at", state.get("updated_at") or now())
    state.setdefault("started_at", state.get("created_at"))
    # 旧运行没有记录回执协议：显式标成 receipt-1，历史上缺失的 exhausted 仍按未验证处理。
    state.setdefault("receipt_protocol", receipts.V1)
    state.setdefault("auth", {})
    state.setdefault("budget_used", {})
    state.setdefault("derived_tasks", [])
    state.setdefault("scope_adjustments", [])
    state.setdefault("events", [])
    state.setdefault("platform_state", {})
    scope = state.setdefault("scope", {})
    scope.setdefault("browser_surface", "embedded")
    scope.setdefault("user_scope_confirmed", False)
    scope.setdefault("narrowed_by", "unknown")
    budget = scope.setdefault("budget", {})
    budget.setdefault("per_city", {})
    budget.setdefault("per_channel", {})
    budget.setdefault("per_city_channel", {})
    budget.setdefault("searches_per_city", None)
    for task in state.get("tasks", []):
        task.setdefault("exhausted", None)
        task.setdefault("query_scope", task.get("role_keyword"))
        task.setdefault("filter_fingerprint", filter_fingerprint(task))
        task.setdefault("pagination_chain_id", task.get("browse_url") or task.get("task_id"))
    state["upgraded_from"] = version
    state["upgraded_at"] = now()
    if workspace:
        try:
            from . import access_control
            for channel in scope.get("channels", []):
                view = access_control.cooldown(workspace, channel)
                if view:
                    state["platform_state"][channel] = view
        except Exception:
            pass
    return state


def latest_run_id(workspace):
    root = run_root(workspace)
    if not root.exists():
        return None
    runs = [p for p in root.iterdir() if (p / "state.json").exists()]
    if not runs:
        return None
    return max(runs, key=lambda p: (p / "state.json").stat().st_mtime).name


def resolve_state(workspace, run_id=None):
    rid = run_id or latest_run_id(workspace)
    if not rid:
        raise ValueError("本工作目录还没有运行记录；请先 start-run 创建本轮")
    return load_state(workspace, rid)


def _append_event(state, event):
    event = dict(event)
    event.setdefault("at", now())
    state.setdefault("events", []).append(event)
    return event


# ---------------------------------------------------------------- 登录前置

def _auth_record(state, channel):
    return state.get("auth", {}).get(channel)


def auth_status(state, channel):
    """登录核验状态：只有同一 run、同一渠道、未过期、会话仍有效且证据有效的记录才算通过。"""
    if state.get("scope", {}).get("platform_auth_policy") == "public_only":
        return "public_only"
    record = _auth_record(state, channel)
    if not record:
        return "missing"
    if record.get("status") != "verified":
        return record.get("status", "missing")
    try:
        checked = parse_time(record["checked_at"])
    except (KeyError, ValueError):
        return "missing"
    ttl = int(record.get("ttl_minutes") or 120)
    if _now_dt() - checked > timedelta(minutes=ttl):
        return "expired"
    if record.get("run_id") != state.get("run_id") or record.get("channel") != channel:
        return "mismatch"
    current = (state.get("browser") or {}).get("session_id")
    if current and record.get("backend_session_id") and current != record["backend_session_id"]:
        # 浏览器重建/重开 = 原会话失效，必须重新核验。
        return "invalidated"
    return "verified"


def pending_auth_channels(state, stage=None):
    """当前阶段里还缺登录核验的平台渠道（一次性打开，不逐条弹窗）。"""
    stage = stage or state.get("current_stage")
    out = []
    for channel in state["scope"]["channels"]:
        if channel_stage(channel) != stage or channel not in PLATFORM_CHANNELS:
            continue
        alive = [t for t in state["tasks"] if t["channel"] == channel and t["status"] not in TASK_TERMINAL]
        if not alive:
            continue
        status = auth_status(state, channel)
        if status == "verified":
            continue
        record = _auth_record(state, channel) or {}
        out.append({"channel": channel, "label": channel_registry.label(channel),
                    "login_url": login_url(channel), "auth_status": status,
                    "access_mode": channel_registry.access_mode(channel),
                    "automation_allowed": channel_registry.automation_allowed(channel),
                    "browser_surface": (state.get("scope") or {}).get("browser_surface"),
                    "browser_status": (state.get("browser") or {}).get("status"),
                    "tab_id": record.get("tab_id"),
                    "browser_session_id": record.get("backend_session_id"),
                    "opened": bool((record.get("opened_event") or {}).get("backend_session_id"))})
    return out


def _channel_needs_auth(state, channel):
    if channel not in PLATFORM_CHANNELS:
        return False
    if state.get("scope", {}).get("platform_auth_policy") == "public_only":
        return False
    return auth_status(state, channel) != "verified"


def _platform_auth_blocked_by_policy(state, channel):
    return state.get("scope", {}).get("platform_auth_policy") == "public_only"


def platform_gate(state, channel):
    """平台当前是否可访问：冷却/受限/能力缺口都返回结构化原因。"""
    view = (state.get("platform_state") or {}).get(channel)
    if view:
        return view
    browser = state.get("browser") or {}
    if (state.get("scope") or {}).get("browser_surface") == "external" \
            and channel in PLATFORM_CHANNELS and browser.get("status") == "unavailable":
        return {"state": "capability_gap", "reason": browser.get("reason"),
                "requires_local_connector": browser.get("requires_local_connector", True)}
    return None


# ---------------------------------------------------------------- 预算（统一口径）

def channel_budget_cap(state, channel):
    return budgets.cap((state.get("scope") or {}).get("budget") or {}, channel, None)


def channel_budget_used(state, channel, city):
    return budgets.used(state, channel, city)


def channel_budget_remaining(state, channel, city):
    return budgets.channel_remaining(state, channel, city)


def budget_remaining(state, city):
    """全轮还能执行多少次任务；None 表示计划未设上限。"""
    return budgets.total_remaining(state, city)


def stage_budget_remaining(state, stage, city):
    """阶段口径剩余，仅用于报告（不再作为第二道门）。"""
    return budgets.stage_remaining(state, stage, city)


def task_budget_remaining(state, task):
    """该任务还能不能执行：渠道配额与计量类型上限取更严的一个。"""
    city = task.get("target_city")
    channel = task.get("channel")
    values = [budgets.channel_remaining(state, channel, city)]
    kind = budgets.kind_for(task.get("route"))
    if kind != budgets.SEARCH:
        values.append(budgets.kind_remaining(state, kind, city))
    known = [v for v in values if v is not None]
    if not known:
        return None
    return min(known)


def _incomplete_searches(state, stage):
    return [row["tasks"][0] for row in open_scopes(state, stage)]


def derived_gaps(state, stage=None):
    """官网发现类任务里，产出企业线索但没转成核实任务、也没给不可核实理由的账。"""
    out = []
    for task in state["tasks"]:
        if stage and task["stage"] != stage:
            continue
        if task["route"] not in DISCOVERY_ROUTES or task["status"] != "executed":
            continue
        found = max(int(task.get("candidate_count") or 0), len(task.get("identified") or []))
        registered = len(task.get("derived_ids") or [])
        waived = len(task.get("company_waivers") or [])
        if found > registered + waived:
            out.append({"task_id": task["task_id"], "channel": task["channel"],
                        "found": found, "registered": registered, "waived": waived,
                        "missing": found - registered - waived})
    return out


def next_action(state):
    """返回当前唯一合法的下一动作；调用方不得自行挑选方便的渠道。"""
    order = state["scope"]["stage_order"]
    stage = state.get("current_stage")
    if state.get("finished"):
        return {"action": "finished", "verdict": state.get("completion", {}).get("verdict")}

    pending_task = _next_task(state, stage) if stage else None
    open_work = _incomplete_searches(state, stage) if stage and not pending_task else []
    lead_gaps = derived_gaps(state, stage) if stage and not pending_task else []

    if open_work:
        task = open_work[0]
        return {"action": "continue_channel", "run_id": state["run_id"], "stage": stage,
                "task_id": task["task_id"], "channel": task["channel"], "route": task["route"],
                "scope": scope_of(task),
                "budget_remaining": task_budget_remaining(state, task),
                "message": ("该搜索范围还没声明到底，且预算仍有剩余：如还有下一页/下一批结果，"
                            "用 action=register_derived 派生后续页任务并继续；若确实已到底，"
                            "重新提交该任务回执并带上 exhausted=true（补证据属于修订，不重复计次）")}

    if lead_gaps:
        gap = lead_gaps[0]
        return {"action": "register_derived", "run_id": state["run_id"], "stage": stage,
                "task_id": gap["task_id"], "channel": gap["channel"],
                "missing": gap["missing"],
                "message": (f"{gap['task_id']} 产出 {gap['found']} 条企业线索，只登记了 "
                            f"{gap['registered']} 条核实任务、{gap['waived']} 条不可核实说明；"
                            "请把剩余企业逐家登记为官网核实任务（action=register_derived）")}

    while stage and not open_work and _stage_is_closable(state, stage):
        closed = close_stage(state, stage, persist=False)
        nxt = _advance_stage(state, stage)
        if not nxt:
            state["finished"] = True
            state["completion"] = finalize(state, persist=False)
            return {"action": "finalize", "run_id": state["run_id"],
                    "verdict": state["completion"]["verdict"], "closed_stage": closed}
        stage = nxt
        state["current_stage"] = stage

    earlier = [s for s in order[:order.index(stage)]
               if any(t["stage"] == s and t["status"] not in TASK_TERMINAL for t in state["tasks"])]
    if earlier:
        target = earlier[0]
        state["stages"][target] = {"status": "reopened", "closed_at": None,
                                   "reason": "派生任务并入原阶段，需重新处理"}
        state["current_stage"] = target
        _append_event(state, {"kind": "reopen_stage", "stage": target})
        stage = target

    task = _next_task(state, stage)
    if task:
        gate = platform_gate(state, task["channel"])
        if gate and gate.get("state") == "capability_gap":
            return {"action": "capability_gap", "run_id": state["run_id"], "stage": stage,
                    "channel": task["channel"], "task_id": task["task_id"],
                    "requires_local_connector": gate.get("requires_local_connector"),
                    "reason": gate.get("reason"),
                    "message": ("本轮要求用户本机独立浏览器，但本机探测不到可用浏览器；"
                                "不会回退到 agent 内嵌预览。请在本机安装 Chrome/Edge 或提供本机连接程序，"
                                "记录能力缺口后继续其他渠道")}
        if gate and gate.get("state") in ("cooling", "blocked"):
            return {"action": "channel_blocked" if gate["state"] == "blocked" else "channel_cooling",
                    "run_id": state["run_id"], "stage": stage, "channel": task["channel"],
                    "until": gate.get("until"), "reason": gate.get("reason"),
                    "message": ("该平台处于冷却/受限状态（跨 run 保留）：按阶段规则推进其他可访问渠道，"
                                "到期或用户显式重试后再恢复")}
        if _channel_needs_auth(state, task["channel"]):
            channel = task["channel"]
            record = _auth_record(state, channel) or {}
            # 用户已声明登录但结构证据不足：先给"点进账号页面再核验"的具体指引，
            # 不重复弹登录页，也不把用户的话当成没登录。
            claimed = bool(record.get("user_claim")) or record.get("status") == "reconfirm_required"
            if claimed and record.get("status") != "verified":
                from .platform_auth import reconfirm_steps
                steps = reconfirm_steps(channel, record.get("problems") or record.get("last_problems") or [],
                                        int(record.get("reconfirm_attempts") or 0))
                if steps["attempts"] < steps["limit"]:
                    return {"action": "wait_user_reconfirm", "run_id": state["run_id"],
                            "stage": stage, "channel": channel, "task_id": task["task_id"],
                            "tab_id": record.get("tab_id"),
                            "browser_context_id": record.get("backend_session_id"),
                            "header_signal": record.get("header_signal"),
                            "reconfirm": steps,
                            "message": ("用户已声明登录，但核验还差一项结构证据（见 reconfirm.steps）。"
                                        "请用户在同一个标签页点进账号页面并刷新，然后重新 auth-probe；"
                                        "助手不得自行判定为已登录")}
                if record.get("status") != "waiting_user":
                    return {"action": "open_auth_tab", "run_id": state["run_id"], "stage": stage,
                            "channel": channel, "task_id": task["task_id"],
                            "login_url": login_url(channel),
                            "browser_surface": (state.get("scope") or {}).get("browser_surface"),
                            "browser_status": (state.get("browser") or {}).get("status"),
                            "access_mode": channel_registry.access_mode(channel),
                            "reconfirm": steps,
                            "message": (f"该平台已连续 {steps['attempts']} 次核验未通过（上限 {steps['limit']}）。"
                                        "可重新打开登录页再试；若仍不通过，建议记 blocked 并推进其他渠道，"
                                        "不要把未覆盖算作零岗位")}
            if record.get("status") == "waiting_user":
                return {"action": "wait_user_auth", "run_id": state["run_id"], "stage": stage,
                        "channel": channel, "task_id": task["task_id"],
                        "tab_id": record.get("tab_id"),
                        "browser_context_id": record.get("backend_session_id"),
                        "login_url": record.get("login_url"),
                        "browser_surface": (state.get("scope") or {}).get("browser_surface"),
                        "message": "已在本机独立浏览器打开登录入口，等待用户在同一会话完成登录后提交核验"}
            if record.get("status") == "blocked":
                return {"action": "channel_blocked", "run_id": state["run_id"], "stage": stage,
                        "channel": channel, "reason": record.get("reason"),
                        "message": "该平台登录受阻；记录原因后可跳过该平台，其余工作继续"}
            return {"action": "open_auth_tab", "run_id": state["run_id"], "stage": stage,
                    "channel": channel, "task_id": task["task_id"], "login_url": login_url(channel),
                    "browser_surface": (state.get("scope") or {}).get("browser_surface"),
                    "browser_status": (state.get("browser") or {}).get("status"),
                    "access_mode": channel_registry.access_mode(channel),
                    "message": ("该平台的业务搜索前先在本机独立浏览器打开登录页并核验当前会话"
                                "（用 auth-open 命令，不要用内嵌预览）；未登录则交给用户登录")}

    if task:
        action = "read_entry" if task["route"] in ("enterprise", "entry_reuse",
                                                   "announcement_reverse", "enterprise_fallback") \
            else "search_task"
        return {"action": action, "run_id": state["run_id"], "stage": stage,
                "task_id": task["task_id"], "channel": task["channel"], "route": task["route"],
                "query": task["query"], "company": task["company"],
                "target_city": task["target_city"], "attempts": task["attempts"],
                "scope": scope_of(task), "access_mode": channel_registry.access_mode(task["channel"]),
                "budget_remaining": task_budget_remaining(state, task)}

    if task is None:
        # 有未处置任务但被平台状态挡住：先给结构化原因，不要笼统地 await_resolution。
        for blocked in [t for t in state["tasks"]
                        if t["stage"] == stage and t["status"] not in TASK_TERMINAL]:
            gate = platform_gate(state, blocked["channel"])
            if not gate:
                continue
            if gate.get("state") == "capability_gap":
                return {"action": "capability_gap", "run_id": state["run_id"], "stage": stage,
                        "channel": blocked["channel"], "task_id": blocked["task_id"],
                        "requires_local_connector": gate.get("requires_local_connector"),
                        "reason": gate.get("reason"),
                        "message": ("本轮要求用户本机独立浏览器，但本机探测不到可用浏览器；"
                                    "不会回退到 agent 内嵌预览。请在本机安装 Chrome/Edge 或提供本机连接程序，"
                                    "记录能力缺口后继续其他渠道")}
            if gate.get("state") in ("cooling", "blocked"):
                return {"action": "channel_blocked" if gate["state"] == "blocked"
                        else "channel_cooling",
                        "run_id": state["run_id"], "stage": stage, "channel": blocked["channel"],
                        "until": gate.get("until"), "reason": gate.get("reason"),
                        "message": ("该平台处于冷却/受限状态（跨 run 保留）：按阶段规则推进其他可访问渠道，"
                                    "到期或用户显式重试后再恢复")}

    if _stage_is_closable(state, stage):
        return {"action": "close_stage", "run_id": state["run_id"], "stage": stage}

    pending = [t for t in state["tasks"] if t["stage"] == stage and t["status"] not in TASK_TERMINAL]
    return {"action": "await_resolution", "run_id": state["run_id"], "stage": stage,
            "tasks": [{"task_id": t["task_id"], "channel": t["channel"], "status": t["status"],
                       "reason": t.get("reason")} for t in pending],
            "message": "当前阶段仍有未处置任务：请重试、记录受阻证据或由用户明确跳过"}


def _next_task(state, stage):
    for task in state["tasks"]:
        if task["stage"] != stage:
            continue
        if task["status"] in ("pending", "failed"):
            if task["attempts"] >= MAX_TASK_ATTEMPTS:
                continue
            gate = platform_gate(state, task["channel"])
            if gate and gate.get("state") in ("cooling", "blocked", "capability_gap"):
                continue
            if task_budget_remaining(state, task) == 0:
                continue
            return task
    return None


def _stage_tasks(state, stage):
    return [t for t in state["tasks"] if t["stage"] == stage]


def _stage_is_closable(state, stage):
    tasks = _stage_tasks(state, stage)
    if not tasks:
        return True
    return all(t["status"] in TASK_TERMINAL for t in tasks)


def _advance_stage(state, stage):
    order = state["scope"]["stage_order"]
    index = order.index(stage)
    for nxt in order[index + 1:]:
        if state["stages"].get(nxt, {}).get("status") != "closed":
            return nxt
    return None


# ---------------------------------------------------------------- 提交回执

PLAN_FIELDS = ("task_id", "query", "target_city", "role_keyword", "channel", "route", "stage", "company")
# 兼容旧调用方：真正的字段清单只有 receipts.RESULT_FIELDS 一份。
RECEIPT_ALLOWED = receipts.RESULT_FIELDS


def record_action(state, payload, workspace=None):
    """提交实际执行结果。计划字段不可被回执覆盖。"""
    action = payload.get("action") or payload.get("kind") or "search"
    task = None
    if action in ("search", "search_task", "read_entry") and payload.get("task_id"):
        task = next((t for t in state["tasks"] if t["task_id"] == payload["task_id"]), None)
    normalized = receipts.normalize(payload, task=task, run_protocol=state.get("receipt_protocol"))
    action = normalized["action"]
    if action in ("search", "search_task", "read_entry"):
        return _record_search(state, normalized, workspace)
    if action in ("auth_open", "open_auth_tab"):
        return _record_auth_open(state, normalized, workspace)
    if action in ("auth_probe", "auth_verified", "inspect_session"):
        return _record_auth_probe(state, normalized, workspace)
    if action in ("user_claimed_login", "login_claim"):
        return _record_auth_claim(state, normalized, workspace)
    if action in ("skip", "skipped_by_user"):
        return _record_skip(state, normalized, workspace)
    if action in ("blocked", "channel_blocked"):
        return _record_blocked(state, normalized, workspace)
    if action in ("register_derived", "derive", "register_candidates"):
        return _record_derived(state, normalized, workspace)
    if action == "scope_adjust":
        return _record_scope_adjust(state, normalized, workspace)
    raise ValueError("未知动作：" + str(action))


def _find_task(state, task_id):
    task = next((t for t in state["tasks"] if t["task_id"] == task_id), None)
    if not task:
        raise ValueError("任务不属于本轮运行：" + str(task_id))
    return task


def _assert_current_stage(state, task):
    if task["stage"] != state["current_stage"]:
        allowed = next_action(state)
        raise ValueError(
            f"跨阶段抢跑被拒绝：任务 {task['task_id']} 属于「{STAGE_LABELS.get(task['stage'], task['stage'])}」阶段，"
            f"当前阶段是「{STAGE_LABELS.get(state['current_stage'], state['current_stage'])}」。"
            f"当前允许的动作：{allowed.get('action')}")


def _assert_auth(state, channel):
    if channel not in PLATFORM_CHANNELS:
        return
    if _platform_auth_blocked_by_policy(state, channel):
        return
    status = auth_status(state, channel)
    if status == "verified":
        return
    raise ValueError(
        f"{channel} 尚未完成当前会话登录核验（状态：{status}）；"
        "请先执行 next-action 给出的 open_auth_tab / wait_user_auth / wait_user_reconfirm 流程；"
        "用户若已声明登录但核验不通过，用 action=user_claimed_login 记录声明，"
        "再按 reconfirm 指引让用户点进账号页面后重新 auth-probe。核验通过后才允许该平台业务搜索")


def _record_search(state, receipt, workspace):
    task = _find_task(state, receipt.get("task_id"))

    # 1) 幂等：同一 receipt_id 重放，不扣预算、不改证据时间；预算耗尽、阶段已推进后也照样成功。
    #    幂等判断必须放在阶段/配额检查之前，否则"重放"会被误判成"抢跑"或"超预算"。
    if receipts.is_duplicate(task, receipt):
        return {"recorded": "search", "duplicate": True, "task": _public_task(task),
                "next": next_action(state)}
    _assert_current_stage(state, task)
    # 2) 修订：同一 attempt_id 补充 exhausted / 结束证据 / 派生关系，保留审计，不重复计次。
    revision = receipts.is_revision(task, receipt)
    # 3) 新执行：先查配额，再扣费。
    if not revision:
        limit = budgets.channel_remaining(state, task["channel"], task.get("target_city"))
        if limit == 0:
            raise ValueError(
                f"渠道 {channel_registry.label(task['channel'])} 本轮预算已用完"
                f"（配额 {budgets.cap((state.get('scope') or {}).get('budget') or {}, task['channel'], task.get('target_city'))} 次，"
                f"已用 {budgets.used(state, task['channel'], task.get('target_city'))} 次）。"
                "请改用该渠道已有的读取结果，或记 blocked/skipped_by_user 说明未覆盖部分；"
                "要追加额度请在新一轮 start-run 里提高 budget.per_channel")
        kind = budgets.kind_for(task.get("route"))
        kind_limit = budgets.kind_remaining(state, kind, task.get("target_city"))
        if kind_limit == 0:
            raise ValueError(
                f"该城市 {kind} 计量预算已用完（列表页/详情分别计量，不能靠搜索次数绕过）；"
                "请停止继续打开页面并记录覆盖缺口")

    _assert_auth(state, task["channel"])
    for field in PLAN_FIELDS:
        if field in receipt and receipt[field] not in (None, task.get(field)):
            raise ValueError(
                f"回执不能覆盖计划字段 {field}：计划为 {task.get(field)!r}，回执为 {receipt[field]!r}")

    status = receipt.get("status", "success")
    if status not in receipts.STATUSES:
        raise ValueError("未知执行状态：" + str(status))
    if status in ("success", "failed", "restricted") and not receipt.get("executed_at"):
        raise ValueError("实际执行必须带 executed_at；未执行的查询不能记为执行过")

    normalized_id = receipt.get("identified")
    derived = receipt.get("derived") or []
    waivers = receipt.get("company_waivers") or []
    legacy = bool(receipt.get("legacy"))

    if not legacy and status == "success" and task["route"] in EXHAUSTION_ROUTES \
            and receipt.get("exhausted") is None:
        raise ValueError(
            f"任务 {task['task_id']} 未声明 exhausted：必须说明该检索是否已到底"
            "（无更多页/无更多结果 = true；还有后续页或结果 = false）。"
            "渠道要跑到底，不能只跑第一页就关阶段")

    if not legacy and status == "success" and task["route"] in DISCOVERY_ROUTES:
        by_name = {x["company"]: x.get("url") for x in (normalized_id or [])}
        bad = [w.get("company") for w in waivers if by_name.get(str(w.get("company") or "").strip())]
        if bad:
            raise ValueError(
                "以下企业已找到官网链接，必须逐家核实、不能用 company_waivers 豁免："
                + "、".join(map(str, bad)) +
                "；核实到官网预算耗尽为止（读不到就记 blocked 并附失败证据）")
        found = max(int(receipt.get("candidate_count") or 0), len(normalized_id or []))
        if found > len(derived) + len(waivers):
            raise ValueError(
                f"官网发现类任务 {task['task_id']} 产出 {found} 条企业线索，但只登记了 "
                f"{len(derived)} 条核实任务、{len(waivers)} 条不可核实说明。"
                "研究型检索得到的企业必须逐家转成官网核实任务（derived），"
                "无法核实的要写进 company_waivers 并给理由")

    # 写回任务
    task["receipt_ids"] = list(dict.fromkeys(task.get("receipt_ids", []) + [receipt["receipt_id"]]))
    task["attempt_ids"] = list(dict.fromkeys(task.get("attempt_ids", []) + [receipt["attempt_id"]]))
    task["receipt_version"] = receipt["receipt_version"]
    if not revision:
        task["attempts"] += 1
        task["executed_at"] = receipt.get("executed_at")
        task["search_tool"] = receipt.get("search_tool") or receipt.get("tool")
        task["candidate_count"] = int(receipt.get("candidate_count") or 0)
        task["evidence_ref"] = receipt.get("evidence_ref")
        task["reason"] = receipt.get("reason")
        task["receipt_status"] = status
        task["access_kind"] = budgets.kind_for(task.get("route"))
    else:
        task.setdefault("revisions", []).append({
            "at": now(), "receipt_id": receipt["receipt_id"],
            "fields": sorted(k for k in receipts.RESULT_FIELDS if receipt.get(k) is not None),
        })
        for field in ("evidence_ref", "reason", "candidate_count", "search_mode", "lead_only"):
            if receipt.get(field) is not None:
                task[field] = receipt[field]
        if receipt.get("executed_at"):
            task.setdefault("original_executed_at", task.get("executed_at"))

    if receipt.get("exhausted") is not None:
        task["exhausted"] = bool(receipt["exhausted"])
    elif legacy:
        task["legacy_exhaustion"] = True
        task["exhaustion_note"] = "历史回执未声明检索是否到底，按旧协议接受但不算新版检查通过"
    if waivers:
        task["company_waivers"] = [{"company": w.get("company"), "reason": w.get("reason")}
                                   for w in waivers]
    if normalized_id:
        task["identified"] = normalized_id
        task["candidate_count"] = max(int(task.get("candidate_count") or 0), len(normalized_id))
    if status == "success" and derived:
        created = _create_derived(state, task, derived, kind="company")
        task["derived_ids"] = list(dict.fromkeys(task.get("derived_ids", []) + created))
    if status == "success":
        task["status"] = "executed"
    elif status == "budget_exhausted":
        task["status"] = "budget_exhausted"
    elif status == "not_run":
        task["status"] = "pending"
    else:
        task["status"] = "failed" if task["attempts"] < MAX_TASK_ATTEMPTS else "blocked"
        if task["status"] == "blocked":
            task["reason"] = (task.get("reason") or "") + "（重试耗尽）" if task.get("reason") else "重试耗尽"

    if not revision:
        budgets.charge(state, task, task.get("access_kind"))
    _note_platform(state, workspace, task, status, receipt.get("reason"))
    _append_event(state, {"kind": "search", "task_id": task["task_id"], "stage": task["stage"],
                          "channel": task["channel"], "status": status,
                          "revision": revision, "scope": scope_of(task),
                          "executed_at": task["executed_at"], "tool": task["search_tool"],
                          "candidate_count": task["candidate_count"],
                          "evidence_ref": task["evidence_ref"],
                          "exhausted": task.get("exhausted")})
    nxt = next_action(state)   # 可能自动关闭本阶段并推进；必须在落盘前算完
    if workspace:
        save_state(state, workspace)
    result = {"recorded": "search", "task": _public_task(task), "scope": scope_of(task),
              "next": nxt}
    if revision:
        result["revision"] = True
    return result


def charge_access(state, *, channel, city, kind=budgets.SEARCH, count=1, note=None):
    """为一个不经过任务回执的动作记账（例如浏览器辅助检索的页面读取）。

    仍然走同一本预算账与同一套上限：渠道配额 + 计量类型。超限直接报错，
    不允许"绕过 record-action 就不受预算约束"。
    """
    if kind not in budgets.KINDS:
        raise ValueError("未知计量类型：" + str(kind))
    task = {"channel": channel, "target_city": city, "stage": channel_stage(channel)}
    for _ in range(int(count)):
        if budgets.channel_remaining(state, channel, city) == 0:
            raise ValueError(
                f"渠道 {channels_label(channel)} 本轮预算已用完"
                f"（已用 {budgets.used(state, channel, city)} 次）；请提高配额或减少页面数量")
        if kind != budgets.SEARCH and budgets.kind_remaining(state, kind, city) == 0:
            raise ValueError(
                f"该城市 {kind} 计量预算已用完；浏览器辅助检索不能绕过页面读取上限")
        budgets.charge(state, task, kind, include_search=(kind == budgets.SEARCH))
    _append_event(state, {"kind": "access_charge", "channel": channel, "city": city,
                          "kind": kind, "count": int(count), "note": note})
    return budgets.used(state, channel, city, kind)


def channels_label(name):
    return channel_registry.label(name)


def record_browser_search(state, channel, summary: dict):
    """记录一次浏览器辅助检索（不进任务簿，但进事件与运行状态）。"""
    summary = dict(summary)
    summary.setdefault("at", now())
    state.setdefault("browser_searches", []).append(summary)
    _append_event(state, {"kind": "browser_search", "channel": channel,
                          "keywords": summary.get("keywords"),
                          "location": summary.get("location"),
                          "pages_read": summary.get("pages_read"),
                          "jobs_found": summary.get("jobs_found")})
    return summary


def _note_platform(state, workspace, task, status, reason):
    channel = task["channel"]
    if channel not in PLATFORM_CHANNELS:
        return
    try:
        from . import access_control
    except Exception:
        access_control = None
    if status == "restricted":
        text = str(reason or "")
        server_limited = any(k in text for k in ("429", "频率", "限流", "rate limit",
                                                 "captcha", "验证码", "安全验证", "受限", "拒绝"))
        kind = "rate_limited" if ("429" in text or "频率" in text or "限流" in text.lower()) else \
            "captcha" if ("验证码" in text or "captcha" in text.lower()) else "restricted"
        view = {"state": "blocked" if kind in ("restricted", "captcha") else "cooling",
                "reason": reason, "since": now()}
        state.setdefault("platform_state", {})[channel] = view
        if workspace and access_control and server_limited:
            access_control.note_restricted(workspace, channel, reason=text, kind=kind,
                                           run_id=state.get("run_id"))
    elif status == "success":
        state.setdefault("platform_state", {}).pop(channel, None)
        if workspace and access_control:
            try:
                access_control.note_success(workspace, channel, run_id=state.get("run_id"))
            except Exception:
                pass


def stage_budget_used(state, stage, city):
    return ((state.get("budget_used") or {}).get("by_stage", {}).get(stage, {}) or {}).get(city, 0)


def _task_skeleton(source, *, task_id, route, query, company=None, extra=None):
    row = {"task_id": task_id, "target_city": source.get("target_city"),
           "role_keyword": source.get("role_keyword"), "query": query, "route": route,
           "channel": source["channel"], "stage": source["stage"], "company": company,
           "planned_status": "derived", "status": "pending", "attempts": 0,
           "executed_at": None, "search_tool": None, "candidate_count": 0, "evidence_ref": None,
           "reason": None, "derived_from": source["task_id"], "browse_page": None,
           "browse_url": None, "covers_roles": None, "skipped_stage": False, "exhausted": None,
           "query_scope": source.get("query_scope"),
           "filter_fingerprint": source.get("filter_fingerprint"),
           "pagination_chain_id": source.get("pagination_chain_id")}
    if extra:
        row.update(extra)
    return row


def _create_derived(state, source, items, kind="company"):
    """派生任务与来源任务同阶段。

    `kind=page` 的下一页继承**同一条分页链**，因此末页到底只关闭这条链；
    `kind=company` 的核实任务各有自己的链，互不覆盖。
    """
    created = []
    for item in items:
        seq = len(source.get("derived_ids", [])) + len(created) + 1
        if kind == "page":
            task_id = f'{source["task_id"]}-p{seq}'
            row = _task_skeleton(source, task_id=task_id, route=source["route"],
                                 query=item.get("url") or source["query"],
                                 extra={"browse_url": item.get("url"),
                                        "browse_page": item.get("page"),
                                        "pagination_chain_id": source.get("pagination_chain_id")
                                        or source.get("browse_url") or source["task_id"]})
        else:
            name = (item.get("company") or item.get("name") or "").strip()
            if not name:
                raise ValueError("派生核实任务必须给出企业名称")
            task_id = f'{source["task_id"]}-c{seq}'
            row = _task_skeleton(source, task_id=task_id, route=VERIFY_ROUTE,
                                 query=f'{name} {source.get("target_city") or ""} '
                                       f'{source.get("role_keyword") or ""} 官网 招聘'.replace("  ", " "),
                                 company=name,
                                 extra={"company_url": item.get("url"),
                                        "query_scope": f"verify:{name}",
                                        "pagination_chain_id": task_id})
        if any(t["task_id"] == task_id for t in state["tasks"]):
            continue
        state["tasks"].append(row)
        state.setdefault("derived_tasks", []).append({"task_id": task_id, "from": source["task_id"],
                                                      "kind": kind, "at": now()})
        created.append(task_id)
    _append_event(state, {"kind": "register_derived", "source": source["task_id"],
                          "kind": kind, "tasks": created})
    return created


def _record_derived(state, payload, workspace):
    """把发现到的企业线索/后续页登记为任务；不执行，只入簿。"""
    source = _find_task(state, payload.get("task_id"))
    if source["stage"] != state.get("current_stage"):
        raise ValueError("只能在当前阶段登记派生任务")
    items = payload.get("items") or []
    kind = payload.get("kind") or "company"
    if not items:
        raise ValueError("register_derived 需要 items：[{company,url}] 或 [{url,page}]")
    created = _create_derived(state, source, items, kind=kind)
    source["derived_ids"] = list(dict.fromkeys(source.get("derived_ids", []) + created))
    if kind == "page" and created:
        # 后续页由新任务承接"是否到底"，父任务不再重复计缺口。
        source["continued"] = True
    nxt = next_action(state)
    if workspace:
        save_state(state, workspace)
    return {"recorded": "register_derived", "source": source["task_id"], "created": created, "next": nxt}


def _record_auth_open(state, payload, workspace):
    """记录真实的浏览器打开事件。只打印链接、或只有截图，都会被拒绝。"""
    channel = payload.get("channel")
    if channel not in PLATFORM_CHANNELS:
        raise ValueError("该渠道不需要登录交接：" + str(channel))
    from . import platform_auth
    if payload.get("opened_url_only"):
        raise ValueError("不得把'打印了登录链接'记成'浏览器已打开'；请用 auth-open 实际打开独立窗口")
    handle = {"session_id": payload.get("browser_session_id") or payload.get("browser_context_id"),
              "target_id": payload.get("tab_id"),
              "surface": payload.get("surface"),
              "backend": payload.get("backend"),
              "pid": payload.get("pid"), "profile_dir": payload.get("profile_dir")}
    event = platform_auth.build_open_record(channel, handle,
                                            login_url=payload.get("login_url") or login_url(channel))
    if (payload.get("backend") or "") == platform_auth.HOST_BROWSER:
        event["host_browser_id"] = payload.get("browser_id")
        event["host_tab_id"] = str(payload.get("tab_id"))
        event["host_tool"] = payload.get("host_tool") or "workbuddy_browser"
    if payload.get("opened_at"):
        event["opened_at"] = payload["opened_at"]
    record = {
        "run_id": state["run_id"], "channel": channel,
        "browser_context_id": event["backend_session_id"],
        "backend_session_id": event["backend_session_id"],
        "tab_id": event["tab_id"],
        "login_url": event["login_url"],
        "checked_at": now(), "status": "waiting_user",
        "surface": event["surface"],
        "opened_event": event,
        "evidence_ref": payload.get("evidence_ref"),
        "reason": payload.get("reason") or "已在本机独立浏览器打开登录入口，等待用户完成登录",
    }
    state.setdefault("auth", {})[channel] = record
    if workspace:
        refresh_browser_state(state, workspace)
    _append_event(state, {"kind": "auth_open", "channel": channel,
                          "backend_session_id": record["backend_session_id"],
                          "tab_id": record["tab_id"], "login_url": record["login_url"],
                          "surface": record["surface"]})
    nxt = next_action(state)
    if workspace:
        save_state(state, workspace)
    return {"recorded": "auth_open", "channel": channel, "status": "waiting_user",
            "tab_id": record["tab_id"], "browser_session_id": record["backend_session_id"],
            "next": nxt}


def invalidate_auth(state, channel, reason):
    """会话层面的失效（标签页被关、浏览器重启、账号切换）。

    只让该平台的登录态失效，任务保持 pending：用户重新登录后可继续，
    不会把整个渠道记成受阻。
    """
    record = dict((state.get("auth") or {}).get(channel) or {})
    record.update({"run_id": state["run_id"], "channel": channel, "status": "invalidated",
                   "checked_at": now(), "reason": reason})
    state.setdefault("auth", {})[channel] = record
    _append_event(state, {"kind": "auth_invalidated", "channel": channel, "reason": reason})
    return record


def _record_auth_probe(state, payload, workspace):
    from .platform_auth import verify_probe, reconfirm_steps, verify_host_probe, HOST_BROWSER
    channel = payload.get("channel")
    if channel not in PLATFORM_CHANNELS:
        raise ValueError("该渠道不需要登录核验：" + str(channel))
    previous = (state.get("auth", {}) or {}).get(channel) or {}
    host_mode = ((previous.get("opened_event") or {}).get("backend") == HOST_BROWSER
                 or (payload.get("backend") or "") == HOST_BROWSER)
    if host_mode:
        record, problems = verify_host_probe(state, channel, payload, workspace)
    else:
        record, problems = verify_probe(state, channel, payload, workspace)
    # 打开事件是后续核验的绑定锚点：核验结果覆盖记录时必须保留，否则下一次
    # 快照核验会被判成"没有打开事件"。
    if previous.get("opened_event"):
        record["opened_event"] = previous["opened_event"]
    # 用户已经声明登录：保留声明与重试计数，下一次仍不通过时继续给"点进页面再确认"的指引。
    for key in ("user_claim", "reconfirm_attempts", "last_problems", "last_probe_at"):
        if key in previous:
            record[key] = previous[key]
    if record["status"] != "verified":
        record["reconfirm_attempts"] = int(previous.get("reconfirm_attempts") or 0) + 1
        record["last_problems"] = problems
        record["last_probe_at"] = record.get("checked_at")
    else:
        record.pop("last_problems", None)
    state.setdefault("auth", {})[channel] = record
    _append_event(state, {"kind": "auth_probe", "channel": channel, "status": record["status"],
                          "browser_session_id": record.get("backend_session_id"),
                          "tab_id": record.get("tab_id"), "problems": problems,
                          "header_signal": record.get("header_signal")})
    nxt = next_action(state)
    if workspace:
        save_state(state, workspace)
    result = {"recorded": "auth_probe", "channel": channel, "status": record["status"],
              "problems": problems, "basis": record.get("verification_basis"),
              "header_signal": record.get("header_signal"), "next": nxt}
    if record["status"] != "verified":
        result["message"] = ("核验未通过，不得据此放行该平台业务搜索"
                             + ("；用户已声明登录，下一步会给重新确认指引"
                                if record.get("user_claim") else ""))
        result["reconfirm"] = reconfirm_steps(channel, problems,
                                              int(record.get("reconfirm_attempts") or 0))
    return result


def _record_auth_claim(state, payload, workspace):
    """用户明确表示"我已经登录了"。

    不改变 verified 结论（结构证据不足时仍不放行搜索），只把状态推进到
    `reconfirm_required`，让 next_action 给出"点进账号页面再核验"的具体指引，
    而不是把用户的话当成没登录。
    """
    from . import platform_auth
    channel = payload.get("channel")
    if channel not in PLATFORM_CHANNELS:
        raise ValueError("该渠道不需要登录核验：" + str(channel))
    previous = (state.get("auth", {}) or {}).get(channel) or {}
    if not (previous.get("opened_event") or {}).get("backend_session_id"):
        raise ValueError("还没有真实的浏览器打开事件：请先运行 auth-open，再让用户在该窗口登录")
    claim = previous.get("user_claim") or {}
    record = dict(previous)
    record["user_claim"] = {"claimed_at": now(),
                            "count": int(claim.get("count") or 0) + 1,
                            "note": payload.get("reason") or payload.get("notes")}
    if record.get("status") != "verified":
        record["status"] = "reconfirm_required"
        record["reason"] = "用户声明已登录，但结构化账号区域证据不足；等待用户点进账号页面后重新核验"
    state.setdefault("auth", {})[channel] = record
    _append_event(state, {"kind": "user_claimed_login", "channel": channel,
                          "count": record["user_claim"]["count"], "status": record["status"]})
    if workspace:
        save_state(state, workspace)
    steps = platform_auth.reconfirm_steps(channel, record.get("problems") or [],
                                          int(record.get("reconfirm_attempts") or 0))
    return {"recorded": "user_claimed_login", "channel": channel, "status": record["status"],
            "claim_count": record["user_claim"]["count"], "reconfirm": steps,
            "next": next_action(state)}


def _record_skip(state, payload, workspace):
    if not payload.get("user_confirmed"):
        raise ValueError("跳过必须由用户明确选择（user_confirmed=true），助手不能自行跳过")
    channel = payload.get("channel")
    task_id = payload.get("task_id")
    reason = payload.get("reason") or "用户明确跳过"
    if task_id:
        task = _find_task(state, task_id)
        task["status"] = "skipped_by_user"
        task["reason"] = reason
        targets = [task]
    elif channel:
        targets = [t for t in state["tasks"] if t["channel"] == channel and t["status"] not in TASK_TERMINAL]
        for task in targets:
            task["status"] = "skipped_by_user"
            task["reason"] = reason
        if channel in PLATFORM_CHANNELS:
            state.setdefault("auth", {})[channel] = {
                "run_id": state["run_id"], "channel": channel, "status": "skipped_by_user",
                "checked_at": now(), "reason": reason}
    else:
        raise ValueError("skip 需要 task_id 或 channel")
    state.setdefault("scope_adjustments", []).append(
        {"kind": "skip", "channel": channel, "task_id": task_id, "reason": reason, "at": now()})
    _append_event(state, {"kind": "skip", "channel": channel, "task_id": task_id, "reason": reason,
                          "tasks": [t["task_id"] for t in targets]})
    nxt = next_action(state)
    if workspace:
        save_state(state, workspace)
    return {"recorded": "skip", "tasks": [t["task_id"] for t in targets], "next": nxt}


def _record_blocked(state, payload, workspace):
    channel = payload.get("channel")
    evidence = payload.get("evidence_ref") or payload.get("evidence")
    if not evidence:
        raise ValueError("受阻必须提供能力检测或访问失败证据（evidence_ref）")
    reason = payload.get("reason") or "受阻"
    task_id = payload.get("task_id")
    if task_id:
        single = _find_task(state, task_id)
        if single["status"] in TASK_TERMINAL:
            raise ValueError("任务已处置，不能再记受阻：" + task_id)
        targets = [single]
    else:
        targets = [t for t in state["tasks"] if t["channel"] == channel and t["status"] not in TASK_TERMINAL]
    for task in targets:
        task["status"] = "blocked"
        task["reason"] = reason
        task["evidence_ref"] = evidence
    if channel in PLATFORM_CHANNELS:
        state.setdefault("auth", {})[channel] = {
            "run_id": state["run_id"], "channel": channel, "status": "blocked",
            "checked_at": now(), "reason": reason, "evidence_ref": evidence}
    _append_event(state, {"kind": "blocked", "channel": channel, "reason": reason,
                          "evidence_ref": evidence, "tasks": [t["task_id"] for t in targets]})
    nxt = next_action(state)
    if workspace:
        save_state(state, workspace)
    return {"recorded": "blocked", "channel": channel, "tasks": [t["task_id"] for t in targets],
            "next": nxt}


def _record_scope_adjust(state, payload, workspace):
    if not payload.get("user_confirmed"):
        raise ValueError("范围调整必须由用户明确确认")
    state.setdefault("scope_adjustments", []).append(
        {"kind": "scope", "detail": payload.get("detail"), "reason": payload.get("reason"),
         "at": now(), "user_confirmed": True})
    _append_event(state, {"kind": "scope_adjust", "detail": payload.get("detail")})
    if workspace:
        save_state(state, workspace)
    return {"recorded": "scope_adjust", "next": next_action(state)}


def _public_task(task):
    out = {k: task.get(k) for k in ("task_id", "stage", "channel", "route", "status", "attempts",
                                    "executed_at", "candidate_count", "reason")}
    out["exhausted"] = task.get("exhausted")
    if task.get("derived_ids"):
        out["derived_ids"] = list(task["derived_ids"])
    out["scope"] = scope_of(task)
    return out


# ---------------------------------------------------------------- 阶段与整轮

def close_stage(state, stage, *, persist=True, workspace=None):
    tasks = _stage_tasks(state, stage)
    # 还没派出去的任务，若所属渠道配额已用完，就不能再挂着：如实转 budget_exhausted
    # （计为缺口，不是"已完成"）。口径与派发完全一致：渠道配额 + 计量类型上限。
    for task in tasks:
        if task["status"] not in ("pending", "failed"):
            continue
        if task_budget_remaining(state, task) == 0:
            task["status"] = "budget_exhausted"
            task["reason"] = budgets.exhausted_note(state, task)
    unhandled = [t["task_id"] for t in tasks if t["status"] not in TASK_TERMINAL]
    if unhandled:
        raise ValueError("阶段尚未结束：未处置任务 " + "、".join(unhandled))
    open_work = open_scopes(state, stage)
    if open_work:
        names = "、".join(
            f"{row['channel']}·{row['city']}·{row['query_scope'] or '全部岗位'}"
            f"(余 {task_budget_remaining(state, row['tasks'][0])} 次)"
            for row in open_work)
        raise ValueError(
            "阶段不能关闭：以下搜索范围未声明检索到底且预算仍有剩余 —— " + names +
            "；请用 register_derived 派生下一页/下一轮任务继续检索，或补交 exhausted=true 说明已到底")
    # 预算已经用尽的未完成检索，如实记成 budget_exhausted（可接受收尾，但算缺口）。
    # 例外：同一搜索范围已有任务声明 exhausted=true（该范围整体跑到尾）。
    done_scopes = exhausted_scope_keys(state, stage)
    for task in _stage_tasks(state, stage):
        if scope_key(task) in done_scopes:
            continue
        if task["status"] == "executed" and task["route"] in EXHAUSTION_ROUTES \
                and task.get("exhausted") is False \
                and task_budget_remaining(state, task) == 0:
            task["status"] = "budget_exhausted"
            task["reason"] = budgets.exhausted_note(state, task)
    gaps = derived_gaps(state, stage)
    if gaps:
        detail = "、".join(f"{g['task_id']} 还差 {g['missing']} 家" for g in gaps)
        raise ValueError("阶段不能关闭：官网发现的企业线索未逐家核实 —— " + detail +
                         "；请用 register_derived 登记核实任务，或对这些企业给出 company_waivers 理由")
    statuses = {t["status"] for t in tasks}
    status = "closed"
    if "blocked" in statuses or "skipped_by_user" in statuses or "budget_exhausted" in statuses:
        status = "closed_with_gaps"
    if not tasks:
        status = "closed_empty"
    state["stages"][stage] = {"status": status, "closed_at": now(), "reason": None,
                              "counts": _count(tasks),
                              "scopes": scope_completion(state, stage)}
    _append_event(state, {"kind": "close_stage", "stage": stage, "status": status})
    if persist and workspace:
        save_state(state, workspace)
    return {"stage": stage, "status": status, "counts": _count(tasks)}


def _count(tasks):
    out = {"total": len(tasks), "executed": 0, "blocked": 0, "skipped_by_user": 0,
           "budget_exhausted": 0, "pending": 0, "failed": 0}
    for task in tasks:
        status = task["status"]
        read = status == "executed" or (status == "budget_exhausted"
                                        and task.get("receipt_status") == "success")
        if read:
            out["executed"] += 1
        if status in out and status != "executed":
            out[status] += 1
        elif not read:
            out["pending"] += 1
    return out


def finalize(state, *, persist=True, workspace=None):
    """整轮完成判定。只有这里可以宣称 COMPLETE。"""
    channels_ = [c for c in state["scope"]["channels"] if c not in NON_STAGE_CHANNELS]
    stage_rows, channel_rows, unfinished = [], [], []
    scopes = scope_completion(state)
    for stage in state["scope"]["stage_order"]:
        tasks = _stage_tasks(state, stage)
        counts = _count(tasks)
        closed = state["stages"].get(stage, {}).get("status")
        stage_rows.append({"stage": stage, "label": STAGE_LABELS.get(stage, stage),
                           "status": closed or ("in_progress" if tasks else "pending"),
                           "closed_at": state["stages"].get(stage, {}).get("closed_at"), **counts})
    for channel in channels_:
        tasks = [t for t in state["tasks"] if t["channel"] == channel]
        counts = _count(tasks)
        auth = auth_status(state, channel) if channel in PLATFORM_CHANNELS else "not_required"
        reasons = sorted({t.get("reason") for t in tasks if t.get("reason")})
        channel_scopes = [s for s in scopes if s["channel"] == channel]
        not_exhausted = [s for s in channel_scopes if not s["exhausted"] and s["executed"]]
        channel_rows.append({
            "channel": channel, "stage": channel_stage(channel), **counts,
            "budget_cap": budgets.cap((state.get("scope") or {}).get("budget") or {},
                                      channel, (state["scope"].get("cities") or [None])[0]),
            "budget_used": budgets.used(state, channel, (state["scope"].get("cities") or [None])[0]),
            "list_page_used": budgets.used(state, channel, (state["scope"].get("cities") or [None])[0],
                                           budgets.LIST_PAGE),
            "detail_read_used": budgets.used(state, channel, (state["scope"].get("cities") or [None])[0],
                                             budgets.DETAIL_READ),
            "not_exhausted": len(not_exhausted),
            "scopes": len(channel_scopes),
            "access_mode": channel_registry.access_mode(channel),
            "automation_allowed": channel_registry.automation_allowed(channel),
            "status": "not_covered" if counts["total"] == 0 else "recorded",
            "auth_status": auth,
            "auth_checked_at": (state.get("auth", {}).get(channel) or {}).get("checked_at"),
            "platform_state": (state.get("platform_state") or {}).get(channel),
            "reason": "；".join(reasons) if reasons else None,
        })
        for task in tasks:
            if task["status"] not in TASK_TERMINAL:
                unfinished.append({"task_id": task["task_id"], "channel": channel,
                                   "stage": task["stage"], "status": task["status"],
                                   "reason": task.get("reason") or "未执行"})
    executed = sum(r["executed"] for r in channel_rows)
    gaps = [r for r in channel_rows if r["blocked"] or r["skipped_by_user"] or r["budget_exhausted"]]
    pending = [r for r in channel_rows if r["pending"] or r["failed"]]
    uncovered = [r["channel"] for r in channel_rows if r.get("status") == "not_covered"]
    unverified_auth = [r["channel"] for r in channel_rows
                       if r["auth_status"] in ("missing", "waiting_user", "expired", "mismatch",
                                               "invalidated", "failed", "reconfirm_required")
                       and r["total"] > 0]
    reasons = []
    if unfinished:
        verdict = "WAITING_USER" if any(t["status"] == "pending" for t in unfinished) else "IN_PROGRESS"
        reasons.append(f"仍有 {len(unfinished)} 个任务未处置")
    elif unverified_auth:
        verdict = "WAITING_USER"
        reasons.append("尚有平台未完成当前会话登录核验：" + "、".join(unverified_auth))
    elif gaps:
        verdict = "PARTIAL"
        reasons.append("存在受阻/用户跳过/预算缺口的渠道：" +
                       "、".join(f"{r['channel']}({r['blocked']}受阻,{r['skipped_by_user']}跳过,"
                                 f"{r['budget_exhausted']}预算)" for r in gaps))
    elif executed == 0:
        verdict = "PARTIAL"
        reasons.append("本轮没有任何实际执行的任务，不能凭空回执视为完成")
    elif pending:
        verdict = "PARTIAL"
        reasons.append("存在未处置渠道")
    else:
        verdict = "COMPLETE"
        reasons.append("本轮声明范围内的搜索范围均有可核验的处置结论；"
                       "只表示声明范围完成，不代表该网站全部岗位已查尽")

    lead_gaps = derived_gaps(state)
    if lead_gaps:
        if verdict == "COMPLETE":
            verdict = "PARTIAL"
        reasons.append("官网发现的企业线索未逐家核实：" +
                       "、".join(f"{g['task_id']} 差 {g['missing']} 家" for g in lead_gaps))
    # 升级前跑的任务、以及旧协议回执，都没有"是否搜到底"的记录：一律按未验证处理。
    unverified = [t for t in state["tasks"]
                  if t["route"] in EXHAUSTION_ROUTES
                  and t["status"] in ("executed", "budget_exhausted")
                  and t.get("exhausted") is None]
    if unverified:
        if verdict == "COMPLETE":
            verdict = "PARTIAL"
        reasons.append(f"{len(unverified)} 条检索未声明是否搜到底（升级前记录或旧协议回执），标记为未验证")
    not_exhausted = [s for s in scopes if not s["exhausted"] and s["executed"]]
    if not_exhausted:
        if verdict == "COMPLETE":
            verdict = "PARTIAL"
        detail = "、".join(f"{s['channel']}/{s['city']}/{s['query_scope'] or '全部'}"
                           f"({s['status']})" for s in not_exhausted[:8])
        reasons.append("以下搜索范围未声明检索到底：" + detail)
    if uncovered and verdict == "COMPLETE":
        verdict = "PARTIAL"
        reasons.append("以下启用渠道没有任何任务与执行记录，不能计入完成：" + "、".join(uncovered))
    scope = state.get("scope", {})
    missing_stages = scope.get("missing_stages") or []
    if missing_stages and verdict == "COMPLETE":
        if scope.get("user_scope_confirmed"):
            reasons.append("本轮为用户明确收窄的范围，未覆盖阶段：" +
                           "、".join(STAGE_LABELS.get(x, x) for x in missing_stages))
        else:
            verdict = "PARTIAL"
            reasons.append("本轮范围未经用户确认（助手自行收窄），只覆盖 " +
                           "、".join(STAGE_LABELS.get(x, x) for x in state["scope"]["stage_order"]
                                    if x not in missing_stages) +
                           "；未纳入：" + "、".join(STAGE_LABELS.get(x, x) for x in missing_stages))
    for row in stage_rows:
        if row["status"] not in ("closed", "closed_with_gaps", "closed_empty") and row["total"]:
            if verdict == "COMPLETE":
                verdict = "PARTIAL"
                reasons.append(f"阶段 {row['label']} 尚未关闭")
    stages_open = [r for r in stage_rows
                   if r["status"] not in ("closed", "closed_with_gaps", "closed_empty")]
    blockers = []
    if unfinished:
        blockers.append(f"{len(unfinished)} 个任务未处置：" +
                        "、".join(t["task_id"] for t in unfinished[:6]))
    if stages_open:
        blockers.append("阶段未收尾：" + "、".join(f"{r['label']}({r['status']})" for r in stages_open))
    # 预算已经用尽时，"没到底"是如实的部分覆盖，不再是"还能继续却没做"的阻塞项。
    cities = state["scope"].get("cities") or [None]
    budget_left = min([x for x in (budget_remaining(state, c) for c in cities) if x is not None],
                      default=None)
    if not_exhausted and (budget_left is None or budget_left > 0):
        blockers.append("搜索范围未到底（预算仍有 %s 次）：%s" %
                        (budget_left, "、".join(sorted({s["channel"] for s in not_exhausted}))))
    elif not_exhausted:
        reasons.append("预算已用尽，以下搜索范围未搜到底，按部分覆盖交付：" +
                       "、".join(sorted({s["channel"] for s in not_exhausted})))
    delivery_allowed = not blockers and verdict in ("COMPLETE", "PARTIAL")
    budget_view = budgets.snapshot(state)
    completion = {
        "run_id": state["run_id"],
        "protocol_version": PROTOCOL_VERSION,
        "receipt_protocol": state.get("receipt_protocol"),
        "plan_id": state.get("plan_id"),
        "config_fingerprint": state.get("config_fingerprint"),
        "generated_at": now(),
        "verdict": verdict,
        "reasons": reasons,
        "stage_order": state["scope"]["stage_order"],
        "stages": stage_rows,
        "channels": channel_rows,
        "scopes": scopes,
        "unfinished": unfinished,
        "executed_tasks": executed,
        "total_tasks": len(state["tasks"]),
        "partial_export": verdict != "COMPLETE",
        "delivery_allowed": delivery_allowed,
        "delivery_blockers": blockers,
        "scope_confirmed": bool(scope.get("user_scope_confirmed")),
        "narrowed_by": scope.get("narrowed_by"),
        "browser_surface": scope.get("browser_surface"),
        "access": {"budget": budget_view, "browser": state.get("browser") or {},
                   "platforms": state.get("platform_state") or {},
                   "linkedin": {"access_mode": channel_registry.access_mode("linkedin"),
                                "automation_allowed": channel_registry.automation_allowed("linkedin")}},
        "boundary": "本判定只覆盖经过本控制器记录的动作；未接入控制器的外部工具动作不在统计内。",
        "next_action": None if verdict == "COMPLETE" else next_action(state),
    }
    state["completion"] = completion
    if persist and workspace:
        save_state(state, workspace)
        (state_file(workspace, state["run_id"]).parent / "completion.json").write_text(
            json.dumps(completion, ensure_ascii=False, indent=2), encoding="utf-8")
    return completion


def finish_run(state, workspace=None):
    while True:
        action = next_action(state)
        if action["action"] == "close_stage":
            close_stage(state, state["current_stage"], workspace=workspace)
            nxt = _advance_stage(state, state["current_stage"])
            if not nxt:
                state["finished"] = True
                return finalize(state, workspace=workspace)
            state["current_stage"] = nxt
            if workspace:
                save_state(state, workspace)
            continue
        return finalize(state, workspace=workspace)


def login_url(channel):
    return channel_registry.login_url(channel)


# ---------------------------------------------------------------- 与新检索的衔接

def record_plan_run(state, plan):
    """把 start-run 之后由 followup/extend-plan 派生出来的任务并入同一运行。"""
    known = {t["task_id"] for t in state["tasks"]}
    added = []
    for task in plan.get("tasks", []):
        if task["task_id"] in known:
            continue
        row = _task_from_plan(task, state["scope"]["channels"], derived_from="derived_plan")
        if row["stage"] == "skipped":
            continue
        state["tasks"].append(row)
        state.setdefault("derived_tasks", []).append(row["task_id"])
        added.append(row["task_id"])
    return added


def tasks_for_receipt(state):
    """把控制器里的任务投影回计划格式，供 validate_receipt 复用。"""
    out = []
    for task in state["tasks"]:
        if task.get("skipped_stage"):
            continue
        out.append({k: task.get(k) for k in ("task_id", "target_city", "role_keyword", "query",
                                             "route", "channel", "company", "stage")}
                   | {"status": PLAN_STATUS_BACK.get(task["status"], "planned"),
                      "reason": task.get("reason")})
    return out


PLAN_STATUS_BACK = {
    "pending": "planned",
    "executed": "success",
    "failed": "failed",
    "blocked": "restricted",
    "skipped_by_user": "not_run",
    "budget_exhausted": "budget_exhausted",
}


def claim_receipts(state, logs, *, run_id=None):
    """按 run_id + task_id + attempt_id/receipt_id 去重，支持同一轮分批提交。

    返回 (accepted, duplicates)。重复项被忽略而不是整批拒绝：同一批回执重放应当
    幂等成功，第二批只提交新任务也应当继续累计。
    """
    run_id = run_id or state.get("run_id")
    ledger = state.setdefault("receipt_ledger", {})
    accepted, duplicates = [], []
    for row in logs:
        task_id = row.get("task_id")
        attempt = row.get("attempt_id") or row.get("executed_at") or ""
        receipt_id = row.get("receipt_id") or ""
        key = f"{run_id}|{task_id}|{attempt}|{receipt_id}"
        row = dict(row)
        row.setdefault("run_id", run_id)
        row.setdefault("attempt_id", attempt)
        row.setdefault("receipt_id", receipt_id)
        if key in ledger:
            duplicates.append({"task_id": task_id, "key": key,
                               "first_seen": ledger[key].get("at")})
            continue
        ledger[key] = {"at": now(), "task_id": task_id, "attempt_id": attempt,
                       "receipt_id": receipt_id}
        accepted.append(row)
    return accepted, duplicates


def merge_receipt(state, receipt, config, plan):
    """把回执并入运行状态，同时保留原有 validate_receipt 的字段与预算校验。

    新版回执的 `exhausted/identified/derived/company_waivers` 必须原样保留，
    不能在投影到任务时被字段清单过滤掉。
    """
    from .planning import validate_receipt
    enriched_plan = dict(plan)
    enriched_plan["tasks"] = tasks_for_receipt(state)
    protocol = state.get("receipt_protocol")
    logs = validate_receipt(config, enriched_plan, receipt, run_protocol=protocol)
    recorded = []
    for log in logs:
        if log.get("status") == "not_run":
            continue
        task = next((t for t in state["tasks"] if t["task_id"] == log["task_id"]), None)
        if task is None:
            continue
        payload = {k: log.get(k) for k in receipts.RESULT_FIELDS if log.get(k) is not None}
        payload.update({"action": "search", "task_id": log["task_id"],
                        "receipt_version": receipt.get("receipt_version") or protocol,
                        "receipt_id": log.get("receipt_id"),
                        "attempt_id": log.get("attempt_id")})
        _record_search(state, receipts.normalize(payload, task=task, run_protocol=protocol), None)
        recorded.append(log["task_id"])
    return logs, recorded


def state_from_plan_logs(plan, logs, config=None, reports=None):
    """没有 start-run 时，用计划＋回执临时重建一个只读运行快照。"""
    channels_ = list(plan.get("channels") or (config.sources if config else []))
    order = stage_order(channels_)
    started = plan.get("created_at") or now()
    state = {
        "schema_version": STATE_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "receipt_protocol": receipts.V1,
        "run_id": plan.get("run_id") or ("adhoc-" + (str(plan.get("plan_id"))[:8]
                                                      if plan.get("plan_id") else uuid.uuid4().hex[:8])),
        "created_at": started,
        "started_at": started,
        "updated_at": now(),
        "plan_id": plan.get("plan_id"),
        "config_fingerprint": plan.get("config_fingerprint"),
        "scope": {"channels": channels_, "stage_order": order,
                  "platform_auth_policy": "public_only",
                  "browser_surface": "embedded",
                  "cities": [], "roles": [], "user_scope_explicit": False,
                  "user_scope_confirmed": False, "narrowed_by": "unknown",
                  "missing_stages": [], "budget": budgets.freeze(plan)},
        "current_stage": order[0] if order else "official",
        "stages": {s: {"status": "pending", "closed_at": None} for s in order},
        "tasks": [], "auth": {}, "budget_used": {}, "events": [], "derived_tasks": [],
        "scope_adjustments": [], "reports": reports or [],
    }
    for task in plan.get("tasks", []):
        state["tasks"].append(_task_from_plan(task, channels_))
    for task in plan.get("entry_tasks", []):
        state["tasks"].append(_task_from_plan(task, channels_, derived_from="plan.entry_tasks"))
    logged = {x["task_id"]: x for x in logs}
    for task in state["tasks"]:
        log = logged.get(task["task_id"])
        if not log:
            continue
        status = log.get("status")
        task["executed_at"] = log.get("executed_at")
        task["search_tool"] = log.get("search_tool")
        task["candidate_count"] = int(log.get("candidate_count") or 0)
        task["reason"] = log.get("reason")
        task["attempts"] = 1 if status in ("success", "failed", "restricted") else 0
        if log.get("exhausted") is not None:
            task["exhausted"] = bool(log["exhausted"])
        task["status"] = {"success": "executed", "failed": "failed", "restricted": "blocked",
                          "budget_exhausted": "budget_exhausted"}.get(status, "pending")
        if status == "failed" and task["attempts"] < MAX_TASK_ATTEMPTS:
            task["status"] = "failed"
    return state


def completion_with_channels(completion, config, reports=None):
    """补齐"配置里启用、但本轮根本没有任务"的渠道，避免被算成已完成。"""
    present = {row["channel"] for row in completion["channels"]}
    reasons_by_channel = {}
    for report in reports or []:
        channel = report.get("channel")
        if channel and report.get("url") is None and report.get("reason"):
            reasons_by_channel.setdefault(channel, report["reason"])
    missing = []
    for channel in (config.sources if config else []):
        if channel in NON_STAGE_CHANNELS or channel in present:
            continue
        missing.append(channel)
        completion["channels"].append({
            "channel": channel, "stage": channel_stage(channel), "total": 0, "executed": 0,
            "blocked": 0, "skipped_by_user": 0, "budget_exhausted": 0, "pending": 0, "failed": 0,
            "not_exhausted": 0, "scopes": 0,
            "access_mode": channel_registry.access_mode(channel),
            "automation_allowed": channel_registry.automation_allowed(channel),
            "auth_status": "not_required" if channel not in PLATFORM_CHANNELS else "missing",
            "auth_checked_at": None, "status": "not_covered",
            "reason": reasons_by_channel.get(channel) or "本轮未生成任务，也未执行任何动作",
        })
    if missing:
        if completion["verdict"] == "COMPLETE":
            completion["verdict"] = "PARTIAL"
            completion["partial_export"] = True
        completion["reasons"].append(
            "以下渠道本轮完全没有处置记录，不能计入完成：" + "、".join(missing))
        if completion.get("next_action") is None:
            completion["next_action"] = {"action": "plan_channel", "channels": missing}
    return completion
