"""招聘平台登录交接与会话核验。

不能只凭"薪资显示数字"判定登录成功：薪资是否披露属于内容完整性。这里由平台适配器
给出**账号专属 UI 标记**，核心自己读取快照文件、自己算哈希、自己匹配标记——助手只能
提交"哪个文件"，不能提交"我已经登录了"。

边界：核验通过的是**用户可见浏览器会话**，不能据此认为无状态 Python HTTP 请求也已
登录；只有 `session_scope=browser_visible_dom` 的读取才在授权范围内。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .models import now

LOGIN_URLS = {
    "zhaopin": "https://passport.zhaopin.com/login",
    "boss": "https://www.zhipin.com/web/user/?ka=header-login",
    "liepin": "https://passport.liepin.com/",
    "51job": "https://login.51job.com/login.php",
    "linkedin": "https://www.linkedin.com/login",
}

# 每个平台在"已登录"状态下才会出现的账号专属 UI 标记；由核心定义，助手不可自定义。
REQUIRED_MARKERS = {
    "zhaopin": [r"我的简历|个人中心|账号设置|退出登录|我的智联"],
    "boss": [r"我的简历|退出登录|我的\{?0\}?消息|求职中心"],
    "liepin": [r"我的猎聘|退出登录|个人中心|我的简历"],
    # 2026-10-31 真实已登录页（we.51job.com/pc/my/myjob）实测：只出现"在线简历/我的申请/我的收藏"等
    # 账号专属入口，没有"我的51job"字样；标记按实测收敛，避免把真实登录误判为未登录。
    "51job": [r"我的51Job|退出登录|在线简历|我的申请|我的收藏|个人中心"],
    "linkedin": [r"My Network|退出|Sign out|Me\s*$"],
}

# 出现这些标记说明仍是登录/校验页面，即使页面可读也不能放行。
BLOCKING_MARKERS = [
    r"请登录|立即登录|登录/注册|扫码登录|密码登录|验证码登录",
    r"人机验证|拖动滑块|安全验证|请输入验证码",
]

PLATFORM_HOSTS = {
    "zhaopin": ("zhaopin.com",),
    "boss": ("zhipin.com",),
    "liepin": ("liepin.com",),
    "51job": ("51job.com",),
    "linkedin": ("linkedin.com",),
}

PROBE_TTL_MINUTES = 120


def platform_host_ok(channel, url):
    host = (url or "").split("//")[-1].split("/")[0].lower()
    return any(host == h or host.endswith("." + h) for h in PLATFORM_HOSTS.get(channel, ()))


def resolve_snapshot(workspace, state, payload):
    """快照必须是本 run 目录内的文件；不接受任意绝对路径。"""
    raw = payload.get("snapshot_path")
    if not raw:
        raise ValueError("缺少 snapshot_path：登录核验必须提交用户可见页面的原始快照文件")
    run_dir = (Path(workspace) / "data" / "runs" / state["run_id"]).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (Path(workspace) / raw).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_relative_to(run_dir):
        raise ValueError("快照必须保存在本轮运行目录 data/runs/<run_id>/ 下，不能指向别处")
    if not candidate.exists():
        raise ValueError("快照文件不存在：" + str(candidate))
    return candidate


def verify_probe(state, channel, payload, workspace):
    problems = []
    session = payload.get("session_scope", "browser_visible_dom")
    if session != "browser_visible_dom":
        problems.append("登录核验只适用于 browser_visible_dom 会话；无状态 HTTP 读取不算登录")
    context = payload.get("browser_context_id")
    tab = payload.get("tab_id")
    if not context or not tab:
        problems.append("缺少 browser_context_id / tab_id，无法证明是同一浏览器会话")
    opened = state.get("auth", {}).get(channel) or {}
    if opened.get("browser_context_id") and context and opened["browser_context_id"] != context:
        problems.append("浏览器会话与 open_auth_tab 记录不一致")
    if opened.get("tab_id") and tab and opened["tab_id"] != tab:
        problems.append("标签页与 open_auth_tab 记录不一致（用户可能关掉了授权标签页）")

    url = payload.get("url") or opened.get("login_url")
    if not platform_host_ok(channel, url):
        problems.append(f"快照 URL 不属于 {channel} 平台")

    snapshot = resolve_snapshot(workspace, state, payload)
    data = snapshot.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    declared = payload.get("dom_hash")
    if declared and declared != digest:
        problems.append("快照哈希与提交值不一致：核验以核心计算的哈希为准")
    text = data.decode("utf-8", "replace")

    markers = REQUIRED_MARKERS.get(channel, [])
    hits = [m for m in markers if re.search(m, text, re.I)]
    if not hits:
        problems.append("快照中没有该平台已登录状态的账号专属 UI 标记（仅有数字薪资不算登录）")
    blocked = [m for m in BLOCKING_MARKERS if re.search(m, text, re.I)]
    if blocked:
        problems.append("快照仍显示登录/验证页面或验证码遮罩：" + "、".join(blocked[:3]))

    record = {
        "run_id": state["run_id"],
        "channel": channel,
        "browser_context_id": context,
        "tab_id": tab,
        "checked_at": now(),
        "status": "verified" if not problems else "failed",
        "session_scope": session,
        "url": url,
        "evidence_ref": str(snapshot),
        "dom_hash": digest,
        "markers_matched": hits,
        "ttl_minutes": PROBE_TTL_MINUTES,
        "problems": problems,
    }
    return record, problems


def capability_report(host):
    """宿主能力探测结果归一化。

    宿主通过 `host` 字典声明能力（例如 WorkBuddy 的浏览器控制开关、扩展连接状态）。
    缺失即记录缺失，不用"打印登录网址"冒充能力存在。
    """
    host = host or {}
    visible = bool(host.get("visible_browser"))
    return {
        "visible_browser": visible,
        "dom_read": bool(host.get("dom_read", visible)),
        "user_handoff": bool(host.get("user_handoff", visible)),
        "session_inspect": bool(host.get("session_inspect", visible)),
        "source": host.get("source") or "宿主自述",
        "status": "available" if visible else "unsupported",
        "reason": None if visible else "宿主未提供用户可见、可操作的浏览器与登录交接能力",
    }


def auth_summary(state):
    from .workflow import PLATFORM_CHANNELS, auth_status
    return {c: auth_status(state, c) for c in state.get("scope", {}).get("channels", [])
            if c in PLATFORM_CHANNELS}
