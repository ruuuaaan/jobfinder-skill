"""版本化回执协议：record-action、new-search --run、浏览器回执共用同一套字段与校验。

为什么需要它
------------
旧实现里回执字段在三处各有一份清单：
- `planning.RECEIPT_RESULT_FIELDS`（8 个字段，没有 exhausted/identified/derived/company_waivers）；
- `workflow.RECEIPT_ALLOWED`（有 derived/company_waivers，却没有 identified）；
- `service.run` 把 `validate_receipt` 的结果再投影一次。
结果是新版回执经过批量入口后 `exhausted`、`identified` 丢掉，完成度判定就拿不到
"是否搜到底"，只能靠旧字段猜。更糟的是旧代码在缺字段时自动写 `legacy_receipt=True`，
把"客户端漏传"变成了"历史协议"。

这里只有一份定义：
- 版本必须显式：`receipt_version` / `protocol_version` / 运行状态里的 `receipt_protocol`；
- `receipt-1` 是历史协议，允许缺 `exhausted`，但会被标记为未验证；
- `receipt-2` 缺必填字段直接报错，**不会**降级；
- `receipt_id` 标识"这一条回执"，`attempt_id` 标识"这一次执行尝试"：
  同 `receipt_id` 重放 = 幂等；同 `attempt_id` 补证据 = 修订；两者都不同 = 新执行。
"""
from __future__ import annotations

import hashlib
import json

V2 = "receipt-2"
V1 = "receipt-1"
VERSIONS = (V1, V2)
LEGACY_VERSIONS = (V1, "receipt-1-inferred", "workflow-1", "workflow-1.0", "workflow-1.1", "1")
CURRENT_VERSIONS = (V2, "workflow-2", "2")

STATUSES = ("success", "failed", "restricted", "budget_exhausted", "not_run")
EXHAUSTION_ROUTES = ("direct", "research", "browse_list", "channel_search")
DISCOVERY_ROUTES = ("direct", "research")

# 回执允许写进任务的结果字段。计划字段（task_id/query/channel/route/stage/company…）不在此列。
RESULT_FIELDS = (
    "status", "executed_at", "search_tool", "candidate_count", "evidence_ref", "reason",
    "search_mode", "lead_only", "exhausted", "identified", "derived", "company_waivers",
    "http_requests", "detail_reads", "observed_at", "role",
)
META_FIELDS = ("receipt_version", "receipt_id", "attempt_id", "legacy_receipt", "action", "kind",
               "task_id", "channel", "run_id", "session_scope", "notes",
               # 宿主浏览器通道：打开事件与核验需要引用真实句柄与后端类型
               "backend", "browser_id", "host_tool", "pid", "profile_dir", "login_url")

ACTIONS = ("search", "read_entry", "search_task", "auth_open", "open_auth_tab", "auth_probe",
           "auth_verified", "inspect_session", "user_claimed_login", "login_claim",
           "skip", "skipped_by_user", "blocked",
           "channel_blocked", "register_derived", "derive", "register_candidates", "scope_adjust")


class ReceiptError(ValueError):
    """回执不合法。消息里必须写清楚缺什么、该补什么。"""


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def resolve_version(payload: dict, run_protocol: str | None = None) -> str:
    """显式识别协议版本；不做"缺字段就当旧协议"的推断。"""
    raw = payload.get("receipt_version") or payload.get("protocol_version")
    if raw is not None:
        text = str(raw).strip().lower()
        if text in CURRENT_VERSIONS:
            return V2
        if text in LEGACY_VERSIONS:
            return V1
        raise ReceiptError(f"未知回执协议版本 {raw!r}；支持 {V2} 与 {V1}")
    if payload.get("legacy_receipt") is True:
        return V1
    if run_protocol in VERSIONS:
        return run_protocol
    return V1


def attempt_id(payload: dict) -> str:
    given = payload.get("attempt_id")
    if given:
        return str(given)
    return "att-" + _digest({"task": payload.get("task_id"), "executed_at": payload.get("executed_at"),
                             "tool": payload.get("search_tool") or payload.get("tool"),
                             "status": payload.get("status")})[:16]


def receipt_id(payload: dict, attempt: str | None = None) -> str:
    given = payload.get("receipt_id")
    if given:
        return str(given)
    return "rcp-" + _digest({
        "task": payload.get("task_id"), "attempt": attempt or attempt_id(payload),
        "status": payload.get("status"), "count": payload.get("candidate_count"),
        "evidence": payload.get("evidence_ref"), "exhausted": payload.get("exhausted"),
        "identified": len(payload.get("identified") or []) if isinstance(payload.get("identified"), list) else None,
        "derived": len(payload.get("derived") or []) if isinstance(payload.get("derived"), list) else None,
        "waivers": len(payload.get("company_waivers") or [])
        if isinstance(payload.get("company_waivers"), list) else None,
    })[:20]


def _as_list(payload: dict, field: str, version: str) -> list:
    value = payload.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReceiptError(f"{field} 必须是数组；收到 {type(value).__name__}")
    if version == V1:
        return value
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ReceiptError(f"{field}[{index}] 必须是对象")
    return value


def normalize(payload: dict, *, task: dict | None = None, run_protocol: str | None = None) -> dict:
    """校验并归一化一条回执；返回可直接交给 workflow 应用的结构。

    `task` 给出计划任务时，会按路由检查 `exhausted` / `identified`；
    缺必填字段在 receipt-2 下一律报错，不降级。
    """
    if not isinstance(payload, dict):
        raise ReceiptError("回执必须是 JSON 对象")
    action = payload.get("action") or payload.get("kind") or "search"
    if action not in ACTIONS:
        raise ReceiptError("未知动作：" + str(action))
    version = resolve_version(payload, run_protocol)
    normalized = {k: payload.get(k) for k in META_FIELDS if k in payload}
    normalized.update({
        "action": action,
        "receipt_version": version,
        "legacy": version == V1,
        "receipt_id": receipt_id(payload),
        "attempt_id": attempt_id(payload),
        "task_id": payload.get("task_id"),
    })
    for field in RESULT_FIELDS:
        if field in payload:
            normalized[field] = payload[field]

    if action in ("search", "read_entry", "search_task"):
        status = payload.get("status", "success")
        if status not in STATUSES:
            raise ReceiptError("未知执行状态：" + str(status))
        normalized["status"] = status
        if status in ("success", "failed", "restricted"):
            if not payload.get("executed_at"):
                raise ReceiptError("实际执行必须带 executed_at；未执行的查询不能记为执行过")
            if version == V2 and not (payload.get("search_tool") or payload.get("tool")):
                raise ReceiptError("receipt-2 的执行回执必须带 search_tool（实际使用的工具或通道）")
        route = (task or {}).get("route") or payload.get("route")
        if version == V2 and status == "success" and route in EXHAUSTION_ROUTES:
            if not isinstance(payload.get("exhausted"), bool):
                raise ReceiptError(
                    f"回执 {payload.get('task_id')} 缺少必填字段 exhausted（boolean）："
                    "该检索是否已到底？无更多页/无更多结果=true；还有后续页或结果=false。"
                    "receipt-2 不会自动降级为旧协议")
        candidate_count = payload.get("candidate_count")
        if candidate_count is not None and not isinstance(candidate_count, int):
            raise ReceiptError("candidate_count 必须是整数")
        identified = payload.get("identified")
        if identified is not None:
            identified = _normalize_identified(identified)
        if version == V2 and status == "success" and route in DISCOVERY_ROUTES:
            needs = route == "research" or int(payload.get("candidate_count") or 0) > 0
            if needs and identified is None:
                raise ReceiptError(
                    f"回执 {payload.get('task_id')} 缺少必填字段 identified："
                    "官网发现类检索必须提交本轮识别到的企业全名单（哪怕不打算核实）")
        normalized["identified"] = identified
        normalized["derived"] = _as_list(payload, "derived", version)
        normalized["company_waivers"] = _as_list(payload, "company_waivers", version)
        if version == V2:
            for index, waiver in enumerate(normalized["company_waivers"]):
                if not str(waiver.get("company") or "").strip():
                    raise ReceiptError(f"company_waivers[{index}] 缺少企业名称")
                if not str(waiver.get("reason") or "").strip():
                    raise ReceiptError(f"company_waivers[{index}] 缺少不可核实理由")
    elif action == "register_derived":
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ReceiptError("register_derived 需要 items：[{company,url}] 或 [{url,page}]")
        normalized["items"] = items
        normalized["kind"] = payload.get("kind") or "company"
        if normalized["kind"] not in ("company", "page"):
            raise ReceiptError("register_derived.kind 只能是 company 或 page")
    elif action in ("auth_open", "open_auth_tab", "auth_probe", "auth_verified", "inspect_session"):
        if not payload.get("channel"):
            raise ReceiptError("登录回执必须带 channel")
        for field in ("browser_context_id", "tab_id", "browser_session_id", "snapshot_path",
                      "url", "session_scope", "opened_at", "surface"):
            if field in payload:
                normalized[field] = payload[field]
    elif action in ("user_claimed_login", "login_claim"):
        if not payload.get("channel"):
            raise ReceiptError("声明已登录必须带 channel")
        normalized["user_confirmed"] = True
        for field in ("reason", "notes"):
            if payload.get(field):
                normalized[field] = payload[field]
    elif action in ("skip", "skipped_by_user"):
        if not payload.get("channel") and not payload.get("task_id"):
            raise ReceiptError("skip 需要 channel 或 task_id")
        normalized["user_confirmed"] = bool(payload.get("user_confirmed"))
    elif action in ("blocked", "channel_blocked"):
        if not payload.get("channel"):
            raise ReceiptError("blocked 必须带 channel")
        normalized["evidence_ref"] = payload.get("evidence_ref") or payload.get("evidence")
    elif action == "scope_adjust":
        normalized["user_confirmed"] = bool(payload.get("user_confirmed"))
    return normalized


def _normalize_identified(items) -> list[dict]:
    if isinstance(items, (str, dict)):
        items = [items]
    out = []
    for item in items or []:
        if isinstance(item, dict):
            name = str(item.get("company") or item.get("name") or "").strip()
            url = (item.get("url") or "").strip() or None
        else:
            name, url = str(item).strip(), None
        if name:
            out.append({"company": name, "url": url})
    return out


def apply_to_task(task: dict, receipt: dict) -> dict:
    """把归一化回执写进任务；只写结果字段。"""
    for field in RESULT_FIELDS:
        if field in receipt and receipt[field] is not None:
            task[field] = receipt[field]
    for field in ("receipt_id", "attempt_id", "receipt_version"):
        task.setdefault(field + "s", [])
        if receipt.get(field) and receipt[field] not in task[field + "s"]:
            task[field + "s"].append(receipt[field])
    return task


def is_duplicate(task: dict, receipt: dict) -> bool:
    return receipt.get("receipt_id") in (task.get("receipt_ids") or [])


def is_revision(task: dict, receipt: dict) -> bool:
    """同一执行尝试的新回执（补 exhausted/结束证据/派生关系）。"""
    if is_duplicate(task, receipt):
        return False
    return receipt.get("attempt_id") in (task.get("attempt_ids") or [])


def describe(receipt: dict) -> dict:
    return {k: receipt.get(k) for k in ("action", "task_id", "status", "exhausted", "receipt_version",
                                       "receipt_id", "attempt_id", "candidate_count")}
