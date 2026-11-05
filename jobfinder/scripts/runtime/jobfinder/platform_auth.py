"""招聘平台登录交接与会话核验。

判定依据只有两条：**真实的浏览器打开事件** + **结构化的账号区域证据**。

旧实现只看"页面文本里有没有账号词汇"，于是：
- 同一篇帮助页/公开公司页里出现 `My Network` 就能通过；
- 打印一个登录网址、再用另一个浏览器读个快照，也能"核验通过"；
- 全页 `请登录` 出现在页脚时按阻断处理，但英文 `Sign in` 完全没识别。

现在：
1. 必须先有 `open_auth_tab` 产生的打开事件（含 `backend_session_id`、`tab_id`、
   `surface=external_browser`），模型只能引用这些句柄，不能自己编；
2. 快照必须是本轮运行目录下的文件，核心自己读、自己算哈希；
3. 登录判定要求**账号区域链接**（header/nav 里的账号入口）与平台标记同时成立，
   全页任意位置出现某个词不算；
4. 登录页、authwall、checkpoint、challenge、验证码遮罩、密码输入框 → 一律不得 verified；
5. 浏览器关闭/上下文重建/退出或换账号后，`backend_session_id` 或页面标记不再匹配，
   核验立即失效；下一次动作前会重新核对。

账号密码、验证码、Cookie、Token 都不进入模型上下文、日志或仓库：浏览器自己持久化会话。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .channels import PLATFORM, login_url as channel_login_url
from .models import now

LOGIN_URLS = {c: channel_login_url(c) for c in PLATFORM}

# 已登录后才出现的账号专属文案（只在 header/nav 区域匹配，不做全页匹配）。
REQUIRED_MARKERS = {
    "zhaopin": [r"我的智联|欢迎回来|我的投递|面试邀请|我的简历|个人中心|账号设置|退出登录"],
    "boss": [r"web/geek/chat|web/geek/resume|我的简历|退出登录|求职中心"],
    "liepin": [r"resume/edit|我的投递|我的收藏|谁看过我|我的猎聘|退出登录|个人中心|我的简历|你好[，,\s]"],
    "51job": [r"我的51Job|退出登录|在线简历|我的申请|我的收藏|个人中心"],
    "linkedin": [r"我的网络|My Network|My network|我的人脉|Messaging|消息|通知|Notifications|Me\b"],
}

# 账号区域链接：必须能在 header/nav 或页面锚点里找到，才认定"已进入账号视图"。
ACCOUNT_LINKS = {
    "zhaopin": [r"/resume", r"/deliver", r"i\.zhaopin\.com", r"/user/", r"/c/i/", r"/my"],
    "boss": [r"web/geek/chat", r"web/geek/resume", r"/web/geek/job", r"/user/"],
    "liepin": [r"resume/edit", r"/my/", r"/user/", r"/c/", r"/zhaopin/"],
    "51job": [r"we\.51job\.com", r"/pc/my", r"/myjob", r"/resume", r"/apply"],
    "linkedin": [r"^/mynetwork", r"^/messaging", r"^/notifications", r"^/me", r"^/jobs", r"^/feed"],
}

# 用户声明"我已经登录了"但结构证据不足时，给出的具体点击指引（按平台）。
# 只描述用户自己要做的动作，助手不代点、不代填、不读凭据。
NAVIGATE_HINTS = {
    "51job": ["点右上角头像，进入「我的51job」（地址栏应变成 we.51job.com/pc/my/myjob）",
              "在该页面停 2–3 秒，等导航栏渲染完"],
    "zhaopin": ["点右上角头像，进入「个人中心」（地址栏应变成 i.zhaopin.com 开头的页面）"],
    "boss": ["点右上角头像，进入「在线简历」或「消息」页面（地址栏应含 web/geek/resume 或 web/geek/chat）"],
    "liepin": ["点「我的猎聘」或右上角头像，进入 c.liepin.com 个人中心"],
    "linkedin": ["点「Me」进入个人主页（地址栏应含 /in/ 或 /mynetwork）"],
}
# 同一渠道允许用户重新确认的次数；超过后提示按受阻处理，不再无限重试。
RECONFIRM_LIMIT = 3

# ---------------------------------------------------------------- 宿主机浏览器通道
# 平台渠道统一走"宿主自己的浏览器工具"（WorkBuddy 内置浏览器 / 扩展连的用户浏览器）：
# 打开登录页 → 用户自己登录 → 用工具在搜索框输入并点搜索 → 读可见 DOM。
# 与专用 profile 的差别：没有本机 BrowserService 可探测，绑定强度靠
# "同一个 browser_id + 同一个 tab_id + 快照新鲜度"来保证，并在记录里显式标注。
HOST_BROWSER = "host_browser"

# 宿主 DOM 是"可见交互节点清单"，不是整页正文：这里只认登录后才出现的账号入口/自称，
# 且必须同时没有登录控件，避免把"整页某处出现一个词"当成登录证据。
HOST_ACCOUNT_WORDS = {
    "51job": [r"我的51job", r"简历中心", r"谁看过我", r"我的申请", r"退出帐号", r"账号设置"],
    "zhaopin": [r"个人中心", r"我的简历", r"我的投递", r"面试邀请", r"消息"],
    "boss": [r"求职中心", r"在线简历", r"我的简历", r"消息"],
    "liepin": [r"我的猎聘", r"我的简历", r"谁看过我", r"我的投递", r"你好[，,\s]"],
    "linkedin": [r"My Network", r"我的人脉", r"Messaging", r"Notifications"],
}
# 出现这些控件说明还没登录（宿主 DOM 里登录入口非常显眼）。
HOST_LOGIN_CONTROLS = [r"登录/注册", r"登录注册", r"请登录", r"立即登录", r"扫码登录", r"密码登录"]
HOST_SNAPSHOT_TTL_MINUTES = 10

# URL 命中即视为登录/风控页面，无论页面文本写了什么。
LOGIN_URL_PATTERNS = [
    r"/login", r"/signin", r"/sign-in", r"/sign_in", r"/authwall", r"/checkpoint",
    r"/challenge", r"/uas/", r"/signup", r"/register", r"/verify", r"/captcha",
    r"passport\.", r"login\.",
]

# 页面出现这些内容说明还在登录/验证流程里。
BLOCKING_MARKERS = [
    r"请登录|立即登录|登录/注册|扫码登录|密码登录|验证码登录|账号登录",
    r"人机验证|拖动滑块|安全验证|请输入验证码|滑动验证|验证码",
    r"\bSign in\b|\bLog in\b|\bJoin now\b|Forgot password|Keep me logged in|New to LinkedIn",
    r"security check|verify you are human|unusual activity|We.?ve restricted",
    r"账号异常|账号受限|账号被限制|请完成验证|身份验证",
]

PLATFORM_HOSTS = {
    "zhaopin": ("zhaopin.com",),
    "boss": ("zhipin.com",),
    "liepin": ("liepin.com",),
    "51job": ("51job.com",),
    "linkedin": ("linkedin.com",),
}

PROBE_TTL_MINUTES = 120
SNAPSHOT_SUFFIXES = (".json", ".html", ".htm", ".txt")


class ProbeError(ValueError):
    """登录核验输入不合法。"""


def platform_host_ok(channel, url):
    host = (url or "").split("//")[-1].split("/")[0].lower()
    return any(host == h or host.endswith("." + h) for h in PLATFORM_HOSTS.get(channel, ()))


def _login_path(url: str) -> str | None:
    match = re.search(r"^[a-z]+://[^/]+(/[^?#]*)", (url or "").lower())
    path = (match.group(1) if match else "/") + ("?" + url.split("?", 1)[1].lower() if "?" in (url or "") else "")
    for pattern in LOGIN_URL_PATTERNS:
        if re.search(pattern, path, re.I):
            return pattern
    return None


def resolve_snapshot(workspace, state, payload):
    """快照必须是本 run 目录内的文件；不接受任意绝对路径。"""
    raw = payload.get("snapshot_path")
    if not raw:
        raise ProbeError("缺少 snapshot_path：登录核验必须提交用户可见页面的原始快照文件")
    run_dir = (Path(workspace) / "data" / "runs" / state["run_id"]).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (Path(workspace) / raw).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_relative_to(run_dir):
        raise ProbeError("快照必须保存在本轮运行目录 data/runs/<run_id>/ 下，不能指向别处")
    if not candidate.exists():
        raise ProbeError("快照文件不存在：" + str(candidate))
    if candidate.suffix.lower() not in SNAPSHOT_SUFFIXES:
        raise ProbeError("快照只接受 json/html/txt：" + candidate.suffix)
    return candidate


def load_snapshot(path: Path) -> dict:
    """读快照。JSON 快照会带上结构信号；HTML 只作为历史/手工兜底（没有账号区域证据）。"""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    text = data.decode("utf-8", "replace")
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ProbeError("快照 JSON 无法解析：" + str(exc)) from exc
        body = str(payload.get("text") or "")
        return {
            "kind": "json",
            "file_hash": digest,
            "text": body,
            "content_hash": payload.get("content_hash") or hashlib.sha256(body.encode()).hexdigest(),
            "url": payload.get("url") or "",
            "title": payload.get("title") or "",
            "anchors": payload.get("anchors") or [],
            "header_text": payload.get("header_text") or "",
            "password_inputs": int(payload.get("password_inputs") or 0),
            "login_forms": int(payload.get("login_forms") or 0),
            "has_captcha_frame": bool(payload.get("has_captcha_frame")),
            "target_id": payload.get("target_id"),
            "browser_session_id": payload.get("session_id"),
            "retrieved_at": payload.get("retrieved_at"),
        }
    # HTML 快照：没有结构信号，按"仅文本"处理，无法通过账号区域检查。
    return {"kind": "text", "file_hash": digest, "text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "url": "", "title": "", "anchors": [], "header_text": "",
            "password_inputs": len(re.findall(r"type=[\"']?password", text, re.I)),
            "login_forms": 0, "has_captcha_frame": False,
            "target_id": None, "browser_session_id": None, "retrieved_at": None}


def _account_link_hits(channel: str, anchors: list[dict]) -> list[str]:
    hits = []
    for pattern in ACCOUNT_LINKS.get(channel, []):
        for anchor in anchors or []:
            href = str(anchor.get("href") or "")
            if re.search(pattern, href, re.I):
                hits.append(f"{pattern}→{href[:80]}")
                break
    return hits


def _header_marker_hits(channel: str, header_text: str, anchors: list[dict]) -> list[str]:
    markers = REQUIRED_MARKERS.get(channel, [])
    candidates = [header_text or ""] + [str(a.get("text") or "") for a in (anchors or [])
                                        if a.get("in_header")]
    joined = "\n".join(candidates)
    return [m for m in markers if re.search(m, joined, re.I)]


def header_signal(snap: dict) -> str:
    """头部信号质量：区分"没抓到"与"抓到了但没匹配"，让重试指引能对症。"""
    if snap.get("kind") != "json":
        return "no_structure"
    text = (snap.get("header_text") or "").strip()
    in_header = [a for a in (snap.get("anchors") or []) if a.get("in_header")]
    if not text and not in_header:
        return "header_not_captured"
    if not text:
        return "header_text_empty"
    return "ok"


def reconfirm_steps(channel: str, problems: list[str] | None = None,
                    attempts: int = 0, limit: int = RECONFIRM_LIMIT) -> dict:
    """用户声明已登录但核验不通过时，给用户的可执行重新确认清单。"""
    steps = list(NAVIGATE_HINTS.get(channel, ["点进右上角头像或「个人中心」页面"]))
    steps += [
        "在同一个标签页按 F5 刷新一次，确认页面完全加载（不要新开标签页，也别换浏览器）",
        "回到对话里回一句「好了」，我会重新提交核验；核验只读页面结构，不读密码、不读 Cookie",
    ]
    return {"attempts": attempts, "limit": limit, "steps": steps,
            "blocking_problems": list(problems or []),
            "note": ("用户已声明登录：这里不推翻用户的话，只说明还差哪一项结构证据；"
                     "重复到上限仍不通过时，按受阻记录并推进其他渠道")}


def binding_problems(state, channel, payload, workspace=None):
    """检查"打开事件 → 当前会话 → 快照"是否同一链路。"""
    problems = []
    opened = (state.get("auth", {}) or {}).get(channel) or {}
    event = opened.get("opened_event") or {}
    if not event.get("backend_session_id") or not event.get("tab_id"):
        problems.append("没有真实的浏览器打开事件（缺 backend_session_id/tab_id）："
                        "仅提交含登录文字的页面快照不能证明用户在本机浏览器中打开了登录页")
        return problems
    if event.get("opened_url_only"):
        problems.append("该记录只是打印了登录链接，没有真正打开浏览器窗口")
    surface = event.get("surface")
    required = (state.get("scope") or {}).get("browser_surface") or "external"
    if surface != "external_browser" and required == "external":
        problems.append(f"本轮要求本机独立浏览器，实际打开形态为 {surface or '未知'}")
    session = payload.get("browser_session_id") or (payload.get("browser_context_id"))
    if session and session != event.get("backend_session_id"):
        problems.append("提交的浏览器会话与打开登录页时不是同一个（可能重开浏览器或换了配置目录）")
    tab = payload.get("tab_id")
    if tab and event.get("tab_id") and tab != event["tab_id"]:
        problems.append("标签页与打开登录页时的记录不一致（用户可能关掉了授权标签页）")
    if workspace and (event.get("backend") or "") != HOST_BROWSER:
        try:
            from .browser_backend import BrowserService
            status = BrowserService(workspace).status()
        except Exception:
            status = {}
        current = status.get("session_id")
        if current and event.get("backend_session_id") and current != event["backend_session_id"]:
            problems.append("浏览器进程已重建（session_id 变化）：登录会话需要重新核验")
        if status.get("status") == "available" and not status.get("running") and \
                event.get("backend_session_id"):
            problems.append("浏览器已关闭：登录会话失效，需要重新打开并登录")
    return problems


def verify_probe(state, channel, payload, workspace):
    """登录核验。返回 (record, problems)；problems 非空即不得放行。"""
    if channel not in PLATFORM:
        raise ProbeError("该渠道不需要登录核验：" + str(channel))
    problems = list(binding_problems(state, channel, payload, workspace))
    opened = (state.get("auth", {}) or {}).get(channel) or {}
    event = opened.get("opened_event") or {}

    session = payload.get("session_scope", "browser_visible_dom")
    if session != "browser_visible_dom":
        problems.append("登录核验只适用于 browser_visible_dom 会话；无状态 HTTP 读取不算登录")

    snapshot_path = resolve_snapshot(workspace, state, payload)
    snap = load_snapshot(snapshot_path)
    declared = payload.get("dom_hash")
    if declared and declared != snap["file_hash"]:
        problems.append("快照哈希与提交值不一致：核验以核心计算的哈希为准")

    url = payload.get("url") or snap.get("url") or event.get("login_url") or channel_login_url(channel)
    if not platform_host_ok(channel, url):
        problems.append(f"快照 URL 不属于 {channel} 平台：{url}")
    login_path = _login_path(url)
    if login_path:
        problems.append(f"当前仍停留在登录/风控页面（URL 命中 {login_path}），不能判定为已登录")

    if snap.get("target_id") and event.get("tab_id") and snap["target_id"] != event["tab_id"]:
        problems.append("快照来自另一个标签页，不是打开登录页的那一个")
    if snap.get("browser_session_id") and event.get("backend_session_id") \
            and snap["browser_session_id"] != event["backend_session_id"]:
        problems.append("快照来自另一个浏览器会话（session_id 不同）")

    text = snap.get("text") or ""
    if snap.get("password_inputs"):
        problems.append("页面仍有密码输入框，说明停在登录表单上，不是已登录视图")
    if snap.get("login_forms"):
        problems.append("页面仍有登录表单提交入口，不能判定为已登录")
    if snap.get("has_captcha_frame"):
        problems.append("页面出现验证码/安全校验组件，需用户本人处理")

    blocked = [m for m in BLOCKING_MARKERS if re.search(m, text, re.I)]
    if blocked:
        problems.append("页面仍显示登录/验证/受限内容：" + "、".join(blocked[:3]))

    link_hits = _account_link_hits(channel, snap.get("anchors") or [])
    marker_hits = _header_marker_hits(channel, snap.get("header_text") or "", snap.get("anchors") or [])
    signal = header_signal(snap)
    if snap["kind"] != "json":
        problems.append("快照缺少结构化信号（anchors/header_text）：无法确认账号区域，"
                        "只凭页面文字不判定登录")
    else:
        if not link_hits:
            problems.append("页面导航中找不到该平台的账号区域链接（如我的投递/消息/个人中心），"
                            "不能只凭全页出现某个词就判定登录")
        if not marker_hits:
            if signal == "header_not_captured":
                problems.append("快照没抓到页面头部文本，也没有标为 header 区域的锚点：该站导航由前端渲染，"
                                "这次只拿到正文与链接。这一项不代表未登录，属于抓不到判定信号")
            elif signal == "header_text_empty":
                problems.append("页面头部文本为空（导航可能还没渲染完或不在首屏），"
                                "无法确认账号视图的标志文案")
            else:
                problems.append("页面头部/导航没有该平台账号视图的标志文案")

    record = {
        "run_id": state["run_id"],
        "channel": channel,
        "browser_context_id": payload.get("browser_context_id") or event.get("backend_session_id"),
        "backend_session_id": event.get("backend_session_id"),
        "tab_id": payload.get("tab_id") or event.get("tab_id"),
        "checked_at": now(),
        "status": "verified" if not problems else "failed",
        "session_scope": session,
        "url": url,
        "surface": event.get("surface"),
        "evidence_ref": str(snapshot_path),
        "dom_hash": snap["file_hash"],
        "markers_matched": marker_hits,
        "account_links_matched": link_hits,
        "header_signal": signal,
        "in_header_anchor_count": len([a for a in (snap.get("anchors") or []) if a.get("in_header")]),
        "ttl_minutes": PROBE_TTL_MINUTES,
        "problems": problems,
        "verification_basis": ["open_event", "session_binding", "url_path", "account_region", "no_login_form"],
    }
    return record, problems


def build_open_record(channel: str, handle: dict, *, login_url: str | None = None) -> dict:
    """由真实浏览器调用结果构造打开事件。缺会话句柄直接报错，不允许"只打印链接"。"""
    if not handle.get("session_id") or not handle.get("target_id"):
        raise ProbeError("打开登录页必须返回真实的 session_id 与 target_id；"
                         "只打印登录链接或截图不算已打开浏览器")
    return {
        "channel": channel,
        "backend": handle.get("backend") or "jobfinder_local_browser",
        "browser_context_id": handle["session_id"],
        "backend_session_id": handle["session_id"],
        "tab_id": handle["target_id"],
        "login_url": login_url or channel_login_url(channel),
        "surface": handle.get("surface") or "external_browser",
        "pid": handle.get("pid"),
        "profile_dir": handle.get("profile_dir"),
        "opened_at": now(),
        "opened_url_only": False,
    }


# ---------------------------------------------------------------- 宿主浏览器回执

_ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)


def parse_host_dom(dom_text: str) -> list[dict]:
    """把宿主返回的可见-DOM 文本解析成 anchors（href + 文本）。"""
    out = []
    for href, label in _ANCHOR_RE.findall(dom_text or ""):
        text = re.sub(r"<[^>]+>", " ", label)
        text = re.sub(r"\s+", " ", text).strip()
        out.append({"href": href.strip(), "text": text[:120], "in_header": False})
    return out


def build_host_snapshot(dom_text: str, *, url: str, title: str = "",
                        host_tool: str = "workbuddy_browser", browser_id=None,
                        tab_id=None) -> dict:
    """由宿主 DOM 文本构造可落盘的快照载荷（核心据此算哈希、做结构化判定）。"""
    text = dom_text or ""
    return {
        "url": url, "title": title, "ready_state": "interactive",
        "text": text,
        "anchors": parse_host_dom(text),
        "header_text": "",
        "password_inputs": len(re.findall(r'type="password"', text, re.I)),
        "login_forms": 0,
        "has_captcha_frame": bool(re.search(r"滑块|人机验证|captcha|challenge", text, re.I)),
        "target_id": str(tab_id) if tab_id is not None else None,
        "retrieved_at": now(),
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "host_tool": host_tool,
        "host_browser_id": browser_id,
        "kind": "host_browser_dom",
        "session_id": f"{host_tool}:{browser_id}" if browser_id is not None else host_tool,
    }


def verify_host_probe(state, channel, payload, workspace=None):
    """宿主浏览器登录核验。

    判定链：真实打开事件（同 browser_id + same tab）→ URL 属于该平台且不在登录页 →
    DOM 里没有登录控件 → DOM 里出现登录后才有的账号入口。
    任何一环缺失都判 failed，并说明缺的是"证据"还是"登录"。
    """
    if channel not in PLATFORM:
        raise ProbeError("该渠道不需要登录核验：" + str(channel))
    problems = []
    opened = ((state.get("auth") or {}).get(channel) or {})
    event = opened.get("opened_event") or {}
    if (event.get("backend") or "") != HOST_BROWSER:
        problems.append("没有宿主浏览器的打开事件：请先用宿主机浏览器打开登录页并登记（host-open）")
    if str(payload.get("browser_id")) != str(event.get("host_browser_id")):
        problems.append("宿主浏览器实例与打开登录页时不同（browser_id 不一致）")
    if str(payload.get("tab_id")) != str(event.get("host_tab_id")):
        problems.append("标签页与打开登录页时不同（tab_id 不一致）：请在同一个标签页里操作")

    snapshot_path = resolve_snapshot(workspace, state, payload)
    snap = load_snapshot(snapshot_path)

    url = payload.get("url") or snap.get("url") or ""
    if not platform_host_ok(channel, url):
        problems.append(f"快照 URL 不属于 {channel} 平台：{url}")
    login_path = _login_path(url)
    if login_path:
        problems.append(f"当前仍停留在登录/风控页面（URL 命中 {login_path}），不能判定为已登录")

    text = snap.get("text") or ""
    if snap.get("password_inputs"):
        problems.append("页面仍有密码输入框，说明停在登录表单上")
    if snap.get("has_captcha_frame"):
        problems.append("页面出现验证码/安全校验，需用户本人处理")
    controls = [m for m in HOST_LOGIN_CONTROLS if re.search(m, text, re.I)]
    if controls:
        problems.append("页面仍有登录入口：" + "、".join(controls[:3]))
    words = [m for m in HOST_ACCOUNT_WORDS.get(channel, []) if re.search(m, text, re.I)]
    links = _account_link_hits(channel, snap.get("anchors") or [])
    if not words and not links:
        problems.append("DOM 里没有登录后才出现的账号入口（如我的简历/投递记录/个人中心），"
                        "无法确认已进入账号视图")

    try:
        from datetime import datetime
        age = (datetime.fromisoformat(now())
               - datetime.fromisoformat(snap.get("retrieved_at") or now())).total_seconds() / 60
    except (TypeError, ValueError):
        age = 0
    if age > HOST_SNAPSHOT_TTL_MINUTES:
        problems.append(f"快照已过期（{age:.0f} 分钟前）：请重新读取当前页面再核验")

    record = {
        "run_id": state["run_id"], "channel": channel,
        "backend": HOST_BROWSER,
        "browser_context_id": event.get("browser_context_id"),
        "backend_session_id": event.get("backend_session_id"),
        "tab_id": str(payload.get("tab_id")),
        "checked_at": now(),
        "status": "verified" if not problems else "failed",
        "session_scope": "browser_visible_dom",
        "url": url,
        "surface": event.get("surface") or "external_browser",
        "evidence_ref": str(snapshot_path),
        "dom_hash": snap["file_hash"],
        "markers_matched": words,
        "account_links_matched": links,
        "header_signal": "host_dom",
        "ttl_minutes": PROBE_TTL_MINUTES,
        "problems": problems,
        "verification_basis": ["host_open_event", "host_tab_binding", "url_path",
                               "account_region_host_dom", "no_login_control"],
        "host_tool": snap.get("host_tool") or "workbuddy_browser",
        "host_browser_id": payload.get("browser_id"),
    }
    return record, problems


def capability_report(workspace=None, host=None):
    """宿主能力：一律以实际探测为准，`host.visible_browser=true` 自述不作数。"""
    detected = {"backend": None, "status": "unsupported", "installed": [], "running": None,
                "capabilities": {}, "reason": "未提供工作目录，无法探测本机浏览器",
                "requires_local_connector": True, "detected_by": "none"}
    if workspace:
        from .browser_backend import capability_report as detect
        detected = detect(workspace)
    report = dict(detected)
    report["host_claim"] = dict(host or {}) or None
    report["host_claim_accepted"] = False
    if host and host.get("visible_browser") and not detected.get("installed"):
        report["reason"] = ("宿主自述有可见浏览器，但本机未探测到 Chrome/Edge："
                            "不接受自述为能力证明")
    return report


def auth_summary(state):
    from .workflow import auth_status
    return {c: auth_status(state, c) for c in state.get("scope", {}).get("channels", [])
            if c in PLATFORM}
