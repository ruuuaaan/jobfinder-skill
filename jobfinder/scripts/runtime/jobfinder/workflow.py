"""统一的运行控制器：阶段顺序、平台登录前置、分批回执与整轮完成判定。

设计边界（必须先读）
--------------------
本模块只约束**经过本项目入口**的动作：`start-run / next-action / record-action /
close-stage / finalize`，以及 `new-search`、`process-browser`、`resume-traversal`
等会读取运行状态的命令。宿主若不调用这些命令、而是自己直接写 Excel 或直接调用
匹配/导出函数，控制器无法阻止它；那种情况下产物里没有 `run_id` 与完成度区块，
应被视为未经控制器验证的结果，而不是"已完成检索"。

数据只写入 `<workspace>/data/runs/<run_id>/`，不写技能安装目录。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .models import now, SHANGHAI

PROTOCOL_VERSION = "workflow-1"

# 全局阶段顺序：官网 → 招聘平台 → 本地宝网站 → 公众号。
# 同一阶段内可以按预算轮转城市/平台/任务；跨阶段不得抢跑。
STAGES = ("official", "recruitment", "bendibao_web", "public_accounts")
STAGE_LABELS = {
    "official": "企业官网",
    "recruitment": "招聘平台",
    "bendibao_web": "本地宝网站",
    "public_accounts": "公众号",
}
CHANNEL_STAGE = {
    "official_career": "official",
    "public_recruitment": "official",
    "zhaopin": "recruitment",
    "boss": "recruitment",
    "liepin": "recruitment",
    "51job": "recruitment",
    "linkedin": "recruitment",
    "bendibao_web": "bendibao_web",
    "bendibao_wechat": "public_accounts",
}
# 需要用户可见浏览器登录交接的渠道（站内搜索与登录态详情必须走这条前置）。
PLATFORM_CHANNELS = ("zhaopin", "boss", "liepin", "51job", "linkedin")
# 非检索类渠道：不进入阶段任务簿，只表示人工材料来源。
NON_STAGE_CHANNELS = ("manual_import",)

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


def stage_order(channels, *, user_selected=False):
    """本轮实际阶段顺序。

    默认四类渠道齐全时就是全局顺序；用户明确只选部分渠道时按所选渠道裁剪，
    不强行补回未选渠道（也不静默改变旧任务的渠道范围）。
    """
    present = {channel_stage(c) for c in channels if c not in NON_STAGE_CHANNELS}
    order = [s for s in STAGES if s in present]
    if not order:
        # 只有人工导入等非检索渠道时，官网阶段仍保留，用于读取用户提供的链接。
        order = ["official"]
    return order


def run_root(workspace):
    return Path(workspace) / "data" / "runs"


def state_file(workspace, run_id):
    return run_root(workspace) / run_id / "state.json"


# ---------------------------------------------------------------- 创建运行

def start_run(config, plan, workspace, *, channels=None, public_only=False,
              auth_policy=None, run_id=None, budget_used=None, previous_run=None):
    """冻结本轮执行范围并落盘。

    `channels` 必须是配置里的渠道子集；不传则取配置渠道。
    `public_only=True` 是显式的匿名读取模式（platform_auth_policy=public_only），
    只能由用户选择，控制器不会因为"图方便"自动降级。
    """
    if plan.get("config_fingerprint") != _plan_fingerprint(plan):
        pass  # 计划自带指纹，配置一致性由 new-search 复核，这里不重复计算
    scope = list(dict.fromkeys(channels or plan.get("channels") or plan.get("effective_config", {}).get("sources", [])))
    unknown = [c for c in scope if c not in NON_STAGE_CHANNELS and c not in CHANNEL_STAGE]
    if unknown:
        raise ValueError("未建模的渠道：" + "、".join(unknown) + "；请在 CHANNEL_STAGE 中显式建模后再启用")
    if public_only:
        policy = "public_only"
    else:
        policy = auth_policy or "required"
    if policy not in ("required", "public_only"):
        raise ValueError("platform_auth_policy 只能是 required 或 public_only")

    plan_tasks = [_task_from_plan(t, scope) for t in plan.get("tasks", [])]
    plan_tasks = [t for t in plan_tasks if t["stage"] != "skipped"]
    entry_tasks = [_task_from_plan(t, scope, derived_from="plan.entry_tasks")
                   for t in plan.get("entry_tasks", [])]
    entry_tasks = [t for t in entry_tasks if t["stage"] != "skipped"]

    run_id = run_id or "run-" + uuid.uuid4().hex[:12]
    state = {
        "schema_version": "workflow-run-1",
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run_id,
        "created_at": now(),
        "updated_at": now(),
        "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("schema_version") or plan.get("plan_version") or "plan-1",
        "config_fingerprint": plan.get("config_fingerprint"),
        "rule_version": plan.get("rule_version"),
        "scope": {
            "cities": list(plan.get("effective_config", {}).get("target_cities") or []),
            "roles": list(plan.get("effective_config", {}).get("role_keywords") or []),
            "channels": scope,
            "stage_order": stage_order(scope),
            "platform_auth_policy": policy,
            "user_scope_explicit": bool(channels),
        },
        "current_stage": stage_order(scope)[0],
        "stages": {s: {"status": "pending", "closed_at": None, "reason": None} for s in stage_order(scope)},
        "tasks": plan_tasks + entry_tasks,
        "auth": {},
        "budget_used": budget_used or {},
        "events": [],
        "derived_tasks": [],
        "scope_adjustments": [],
        "previous_run": previous_run,
        "platform_auth_required": policy == "required",
    }
    state["updated_at"] = now()
    save_state(state, workspace)
    _append_event(state, {"kind": "start_run", "plan_id": state["plan_id"],
                          "channels": scope, "stage_order": state["scope"]["stage_order"],
                          "auth_policy": policy})
    save_state(state, workspace)
    return state


def _plan_fingerprint(plan):
    return plan.get("config_fingerprint")


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
    }
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
    return json.loads(path.read_text(encoding="utf-8"))


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
    """登录核验状态：只有同一 run、同一渠道、未过期且证据有效的记录才算通过。"""
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
    return "verified"


def _channel_needs_auth(state, channel):
    """该渠道此刻是否需要先完成登录核验（只作用于任务自身的平台）。"""
    if channel not in PLATFORM_CHANNELS:
        return False
    if state.get("scope", {}).get("platform_auth_policy") == "public_only":
        return False
    return auth_status(state, channel) != "verified"


def _platform_auth_blocked_by_policy(state, channel):
    return state.get("scope", {}).get("platform_auth_policy") == "public_only"


# ---------------------------------------------------------------- 下一动作

def next_action(state):
    """返回当前唯一合法的下一动作；调用方不得自行挑选方便的渠道。"""
    order = state["scope"]["stage_order"]
    stage = state.get("current_stage")
    if state.get("finished"):
        return {"action": "finished", "verdict": state.get("completion", {}).get("verdict")}
    while stage and _stage_is_closable(state, stage):
        closed = close_stage(state, stage, persist=False)
        nxt = _advance_stage(state, stage)
        if not nxt:
            state["finished"] = True
            state["completion"] = finalize(state, persist=False)
            return {"action": "finalize", "run_id": state["run_id"],
                    "verdict": state["completion"]["verdict"], "closed_stage": closed}
        stage = nxt
        state["current_stage"] = stage

    # 0. 官网阶段后来派生出的任务（official-plan / extend-plan / 后续计划）必须回到官网阶段处理，
    #    不能被追加到招聘平台之后。这里把游标退回去并标记 reopened。
    earlier = [s for s in order[:order.index(stage)]
               if any(t["stage"] == s and t["status"] not in TASK_TERMINAL for t in state["tasks"])]
    if earlier:
        target = earlier[0]
        state["stages"][target] = {"status": "reopened", "closed_at": None,
                                   "reason": "派生任务并入原阶段，需重新处理"}
        state["current_stage"] = target
        _append_event(state, {"kind": "reopen_stage", "stage": target})
        stage = target

    # 1. 当前阶段的可执行任务（含官网派生的企业核实任务）。
    task = _next_task(state, stage)
    # 2. 登录前置绑定到**具体任务所在平台**：一个平台未登录不应阻塞同阶段其他平台。
    if task and _channel_needs_auth(state, task["channel"]):
        channel = task["channel"]
        record = _auth_record(state, channel) or {}
        if record.get("status") == "waiting_user":
            return {"action": "wait_user_auth", "run_id": state["run_id"], "stage": stage,
                    "channel": channel, "task_id": task["task_id"], "tab_id": record.get("tab_id"),
                    "browser_context_id": record.get("browser_context_id"),
                    "login_url": record.get("login_url"),
                    "message": "已打开登录入口，等待用户在同一浏览器会话完成登录后提交核验"}
        if record.get("status") == "blocked":
            return {"action": "channel_blocked", "run_id": state["run_id"], "stage": stage,
                    "channel": channel, "reason": record.get("reason"),
                    "message": "该平台登录受阻；记录原因后可跳过该平台，其余工作继续"}
        return {"action": "open_auth_tab", "run_id": state["run_id"], "stage": stage,
                "channel": channel, "task_id": task["task_id"], "login_url": login_url(channel),
                "message": "该平台的业务搜索前先打开页面并核验当前会话；未登录则交给用户登录"}

    if task:
        action = "read_entry" if task["route"] in ("enterprise", "entry_reuse", "announcement_reverse",
                                                   "enterprise_fallback") else "search_task"
        return {"action": action, "run_id": state["run_id"], "stage": stage,
                "task_id": task["task_id"], "channel": task["channel"], "route": task["route"],
                "query": task["query"], "company": task["company"],
                "target_city": task["target_city"], "attempts": task["attempts"]}

    if _stage_is_closable(state, stage):
        return {"action": "close_stage", "run_id": state["run_id"], "stage": stage}

    # 还有未处置任务但当前无法执行（重试耗尽/待用户决定）。
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
            if task["attempts"] < MAX_TASK_ATTEMPTS:
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
RECEIPT_ALLOWED = ("status", "search_tool", "executed_at", "candidate_count", "evidence_ref",
                   "reason", "search_mode", "lead_only")


def record_action(state, payload, workspace=None):
    """提交实际执行结果。计划字段不可被回执覆盖。"""
    action = payload.get("action") or payload.get("kind")
    if action in (None, "search", "search_task", "read_entry"):
        return _record_search(state, payload, workspace)
    if action in ("auth_open", "open_auth_tab"):
        return _record_auth_open(state, payload, workspace)
    if action in ("auth_probe", "auth_verified", "inspect_session"):
        return _record_auth_probe(state, payload, workspace)
    if action in ("skip", "skipped_by_user"):
        return _record_skip(state, payload, workspace)
    if action in ("blocked", "channel_blocked"):
        return _record_blocked(state, payload, workspace)
    if action == "scope_adjust":
        return _record_scope_adjust(state, payload, workspace)
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
        "请先执行 next-action 给出的 open_auth_tab / wait_user_auth 流程，"
        "核验通过后才允许该平台业务搜索")


def _record_search(state, payload, workspace):
    task = _find_task(state, payload.get("task_id"))
    _assert_current_stage(state, task)
    _assert_auth(state, task["channel"])
    for field in payload:
        if field in PLAN_FIELDS and payload[field] != task.get(field):
            raise ValueError(
                f"回执不能覆盖计划字段 {field}：计划为 {task.get(field)!r}，回执为 {payload[field]!r}")
    status = payload.get("status", "success")
    if status not in ("success", "failed", "restricted", "budget_exhausted", "not_run"):
        raise ValueError("未知执行状态：" + str(status))
    if status in ("success", "failed", "restricted") and not payload.get("executed_at"):
        raise ValueError("实际执行必须带 executed_at；未执行的查询不能记为执行过")
    # 幂等：同一条回执重复提交不重复扣预算、不重复改证据时间。
    receipt_hash = hashlib.sha256(json.dumps(
        {k: payload.get(k) for k in ("task_id", "status", "executed_at", "search_tool", "candidate_count")},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if task.get("receipt_hash") == receipt_hash:
        return {"recorded": "search", "duplicate": True, "task": _public_task(task),
                "next": next_action(state)}
    task["receipt_hash"] = receipt_hash
    task["attempts"] += 1
    task["executed_at"] = payload.get("executed_at")
    task["search_tool"] = payload.get("search_tool") or payload.get("tool")
    task["candidate_count"] = int(payload.get("candidate_count") or 0)
    task["evidence_ref"] = payload.get("evidence_ref")
    task["reason"] = payload.get("reason")
    task["receipt_status"] = status
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
    _append_event(state, {"kind": "search", "task_id": task["task_id"], "stage": task["stage"],
                          "channel": task["channel"], "status": status,
                          "executed_at": task["executed_at"], "tool": task["search_tool"],
                          "candidate_count": task["candidate_count"], "evidence_ref": task["evidence_ref"]})
    _track_budget(state, task)
    if workspace:
        save_state(state, workspace)
    return {"recorded": "search", "task": _public_task(task), "next": next_action(state)}


def _track_budget(state, task):
    if task.get("receipt_status") not in ("success", "failed", "restricted"):
        return
    city = task.get("target_city")
    used = state.setdefault("budget_used", {}).setdefault("searches", {})
    used[city] = used.get(city, 0) + 1


def _record_auth_open(state, payload, workspace):
    channel = payload.get("channel")
    if channel not in PLATFORM_CHANNELS:
        raise ValueError("该渠道不需要登录交接：" + str(channel))
    record = {
        "run_id": state["run_id"], "channel": channel,
        "browser_context_id": payload.get("browser_context_id"),
        "tab_id": payload.get("tab_id"),
        "login_url": payload.get("login_url") or login_url(channel),
        "checked_at": now(), "status": "waiting_user",
        "evidence_ref": payload.get("evidence_ref"),
        "reason": payload.get("reason") or "已打开登录入口，等待用户完成登录",
    }
    state.setdefault("auth", {})[channel] = record
    _append_event(state, {"kind": "auth_open", "channel": channel,
                          "browser_context_id": record["browser_context_id"], "tab_id": record["tab_id"],
                          "login_url": record["login_url"]})
    if workspace:
        save_state(state, workspace)
    return {"recorded": "auth_open", "channel": channel, "status": "waiting_user",
            "next": next_action(state)}


def _record_auth_probe(state, payload, workspace):
    from .platform_auth import verify_probe
    channel = payload.get("channel")
    if channel not in PLATFORM_CHANNELS:
        raise ValueError("该渠道不需要登录核验：" + str(channel))
    record, problems = verify_probe(state, channel, payload, workspace)
    state.setdefault("auth", {})[channel] = record
    _append_event(state, {"kind": "auth_probe", "channel": channel, "status": record["status"],
                          "browser_context_id": record.get("browser_context_id"),
                          "tab_id": record.get("tab_id"), "problems": problems})
    if workspace:
        save_state(state, workspace)
    result = {"recorded": "auth_probe", "channel": channel, "status": record["status"],
              "problems": problems, "next": next_action(state)}
    if record["status"] != "verified":
        result["message"] = "核验未通过，不得据此放行该平台业务搜索"
    return result


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
    if workspace:
        save_state(state, workspace)
    return {"recorded": "skip", "tasks": [t["task_id"] for t in targets], "next": next_action(state)}


def _record_blocked(state, payload, workspace):
    channel = payload.get("channel")
    evidence = payload.get("evidence_ref") or payload.get("evidence")
    if not evidence:
        raise ValueError("受阻必须提供能力检测或访问失败证据（evidence_ref）")
    reason = payload.get("reason") or "受阻"
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
    if workspace:
        save_state(state, workspace)
    return {"recorded": "blocked", "channel": channel, "tasks": [t["task_id"] for t in targets],
            "next": next_action(state)}


def _record_scope_adjust(state, payload, workspace):
    if not payload.get("user_confirmed"):
        raise ValueError("范围调整必须由用户明确确认")
    state.setdefault("scope_adjustments", []).append(
        {"kind": "scope", "detail": payload.get("detail"), "reason": payload.get("reason"),
         "at": now()})
    _append_event(state, {"kind": "scope_adjust", "detail": payload.get("detail")})
    if workspace:
        save_state(state, workspace)
    return {"recorded": "scope_adjust", "next": next_action(state)}


def _public_task(task):
    return {k: task.get(k) for k in ("task_id", "stage", "channel", "route", "status", "attempts",
                                     "executed_at", "candidate_count", "reason")}


# ---------------------------------------------------------------- 阶段与整轮

def close_stage(state, stage, *, persist=True, workspace=None):
    tasks = _stage_tasks(state, stage)
    unhandled = [t["task_id"] for t in tasks if t["status"] not in TASK_TERMINAL]
    if unhandled:
        raise ValueError("阶段尚未结束：未处置任务 " + "、".join(unhandled))
    statuses = {t["status"] for t in tasks}
    status = "closed"
    if "blocked" in statuses or "skipped_by_user" in statuses or "budget_exhausted" in statuses:
        status = "closed_with_gaps"
    if not tasks:
        status = "closed_empty"
    state["stages"][stage] = {"status": status, "closed_at": now(), "reason": None,
                              "counts": _count(tasks)}
    _append_event(state, {"kind": "close_stage", "stage": stage, "status": status})
    if persist and workspace:
        save_state(state, workspace)
    return {"stage": stage, "status": status, "counts": _count(tasks)}


def _count(tasks):
    out = {"total": len(tasks), "executed": 0, "blocked": 0, "skipped_by_user": 0,
           "budget_exhausted": 0, "pending": 0, "failed": 0}
    for task in tasks:
        key = task["status"] if task["status"] in out else "pending"
        out[key] = out.get(key, 0) + 1
    return out


def finalize(state, *, persist=True, workspace=None):
    """整轮完成判定。只有这里可以宣称 COMPLETE。"""
    channels = [c for c in state["scope"]["channels"] if c not in NON_STAGE_CHANNELS]
    stage_rows, channel_rows, unfinished = [], [], []
    for stage in state["scope"]["stage_order"]:
        tasks = _stage_tasks(state, stage)
        counts = _count(tasks)
        closed = state["stages"].get(stage, {}).get("status")
        stage_rows.append({"stage": stage, "label": STAGE_LABELS.get(stage, stage),
                           "status": closed or ("in_progress" if tasks else "pending"),
                           "closed_at": state["stages"].get(stage, {}).get("closed_at"), **counts})
    for channel in channels:
        tasks = [t for t in state["tasks"] if t["channel"] == channel]
        counts = _count(tasks)
        auth = auth_status(state, channel) if channel in PLATFORM_CHANNELS else "not_required"
        reasons = sorted({t.get("reason") for t in tasks if t.get("reason")})
        channel_rows.append({"channel": channel, "stage": channel_stage(channel), **counts,
                             "status": "not_covered" if counts["total"] == 0 else "recorded",
                             "auth_status": auth,
                             "auth_checked_at": (state.get("auth", {}).get(channel) or {}).get("checked_at"),
                             "reason": "；".join(reasons) if reasons else None})
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
                       if r["auth_status"] in ("missing", "waiting_user", "expired", "mismatch")
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
                       "、".join(f"{r['channel']}({r['blocked']}受阻,{r['skipped_by_user']}跳过,{r['budget_exhausted']}预算)" for r in gaps))
    elif executed == 0:
        verdict = "PARTIAL"
        reasons.append("本轮没有任何实际执行的任务，不能凭空回执视为完成")
    elif pending:
        verdict = "PARTIAL"
        reasons.append("存在未处置渠道")
    else:
        verdict = "COMPLETE"
        reasons.append("声明范围内的阶段与渠道均有可核验的处置结论；不代表该网站全部岗位已查完")
    if uncovered and verdict == "COMPLETE":
        verdict = "PARTIAL"
        reasons.append("以下启用渠道没有任何任务与执行记录，不能计入完成：" + "、".join(uncovered))
    for row in stage_rows:
        if row["status"] not in ("closed", "closed_with_gaps", "closed_empty") and row["total"]:
            if verdict == "COMPLETE":
                verdict = "PARTIAL"
                reasons.append(f"阶段 {row['label']} 尚未关闭")
    if state.get("finished") and verdict == "COMPLETE":
        pass
    completion = {
        "run_id": state["run_id"],
        "protocol_version": PROTOCOL_VERSION,
        "plan_id": state.get("plan_id"),
        "config_fingerprint": state.get("config_fingerprint"),
        "generated_at": now(),
        "verdict": verdict,
        "reasons": reasons,
        "stage_order": state["scope"]["stage_order"],
        "stages": stage_rows,
        "channels": channel_rows,
        "unfinished": unfinished,
        "executed_tasks": executed,
        "total_tasks": len(state["tasks"]),
        "partial_export": verdict != "COMPLETE",
        "next_action": None if verdict == "COMPLETE" else next_action(state),
        "boundary": "本判定只覆盖经过本控制器记录的动作；未接入控制器的外部工具动作不在统计内。",
    }
    state["completion"] = completion
    if persist and workspace:
        save_state(state, workspace)
        (state_file(workspace, state["run_id"]).parent / "completion.json").write_text(
            json.dumps(completion, ensure_ascii=False, indent=2), encoding="utf-8")
    return completion


def finish_run(state, workspace=None):
    """允许某个阶段因为无任务而直接推进，直到所有阶段关闭或遇到待处置任务。"""
    while True:
        action = next_action(state)
        if action["action"] in ("close_stage",):
            close_stage(state, state["current_stage"], workspace=workspace)
            nxt = _advance_stage(state, state["current_stage"])
            if not nxt:
                state["finished"] = True
                return finalize(state, workspace=workspace)
            state["current_stage"] = nxt
            if workspace:
                save_state(state, workspace)
            continue
        if action["action"] == "finalize":
            return finalize(state, workspace=workspace)
        return finalize(state, workspace=workspace)


def login_url(channel):
    from .platform_auth import LOGIN_URLS
    return LOGIN_URLS.get(channel)


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
    """把控制器里的任务投影回计划格式，供 validate_receipt 复用。

    必须带 `status`：validate_receipt 会用它判断"超预算任务不能记为成功"。
    """
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


def merge_receipt(state, receipt, config, plan):
    """把回执并入运行状态，同时保留原有 validate_receipt 的字段与预算校验。"""
    from .planning import validate_receipt
    enriched_plan = dict(plan)
    enriched_plan["tasks"] = tasks_for_receipt(state)
    logs = validate_receipt(config, enriched_plan, receipt)
    recorded = []
    for log in logs:
        if log.get("status") == "not_run":
            continue
        task = next((t for t in state["tasks"] if t["task_id"] == log["task_id"]), None)
        if task is None:
            continue
        payload = {k: log.get(k) for k in RECEIPT_ALLOWED if log.get(k) is not None}
        payload.update(action="search", task_id=log["task_id"])
        _record_search(state, payload, None)
        recorded.append(log["task_id"])
    return logs, recorded


def state_from_plan_logs(plan, logs, config=None, reports=None):
    """没有 start-run 时，用计划＋回执临时重建一个只读运行快照。

    这样 new-search / process-browser / 历史入口都能走同一个 finalizer，
    而不是各写一套"看起来成功"的状态。
    """
    channels = list(plan.get("channels") or (config.sources if config else []))
    order = stage_order(channels)
    state = {
        "schema_version": "workflow-run-1",
        "protocol_version": PROTOCOL_VERSION,
        "run_id": plan.get("run_id") or ("adhoc-" + (str(plan.get("plan_id"))[:8] if plan.get("plan_id") else uuid.uuid4().hex[:8])),
        "created_at": plan.get("created_at") or now(),
        "updated_at": now(),
        "plan_id": plan.get("plan_id"),
        "config_fingerprint": plan.get("config_fingerprint"),
        "scope": {"channels": channels, "stage_order": order,
                  "platform_auth_policy": "public_only",
                  "cities": [], "roles": [], "user_scope_explicit": False},
        "current_stage": order[0] if order else "official",
        "stages": {s: {"status": "pending", "closed_at": None} for s in order},
        "tasks": [], "auth": {}, "budget_used": {}, "events": [], "derived_tasks": [],
        "scope_adjustments": [], "reports": reports or [],
    }
    for task in plan.get("tasks", []):
        state["tasks"].append(_task_from_plan(task, channels))
    for task in plan.get("entry_tasks", []):
        state["tasks"].append(_task_from_plan(task, channels, derived_from="plan.entry_tasks"))
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
        task["status"] = {"success": "executed", "failed": "failed", "restricted": "blocked",
                          "budget_exhausted": "budget_exhausted"}.get(status, "pending")
        if status == "failed" and task["attempts"] < MAX_TASK_ATTEMPTS:
            task["status"] = "failed"
    return state


def completion_with_channels(completion, config, reports=None):
    """补齐"配置里启用、但本轮根本没有任务"的渠道，避免被算成已完成。

    这是 service 里 url=None 报告被排除在完成检查之外的修复点：渠道级缺口
    必须显式出现在完成度里，并把 COMPLETE 降级为 PARTIAL。
    """
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
