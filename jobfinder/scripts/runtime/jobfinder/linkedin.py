"""领英（LinkedIn）渠道：访问模式、人工辅助路径与岗位事实保存。

领英与智联/BOSS/前程无忧/猎聘的差别不是"登录方式"，而是**访问许可**：
用户登录成功，只说明他有账号，不说明平台允许自动化抓取。官方帮助页明确禁止
未经许可的抓取软件（https://www.linkedin.com/help/linkedin/answer/a1341387/prohibition-of-scraping-software?lang=en）。
因此本渠道默认只提供两条路径：

1. `assisted_manual`（默认，能力完整可用）：助手在本机独立浏览器里打开领英，
   用户在站内自己搜索、设置关键词与地点；随后用户把岗位原文（或岗位链接 + 原文摘录）
   交给助手，核心只做**事实提取与匹配**，不代抓、不翻页、不调内部接口。
2. `licensed_api`（默认关闭）：只有在用户能给出**明确的平台许可证据**（合作/授权文档、
   官方 API 合同编号等）时才启用；没有证据时 start-run 直接拒绝，不会"先跑起来再说"。

`linkedin_job_posting_api` 不是通用求职搜索接口，本模块不把它当搜索用。

来源与事实规则：
- `source_channel=linkedin`、`source_type=platform`（不能复用官网处理器写死 official_career）；
- 工作地点只用岗位详情原文，不根据设备位置、企业所在地或单独一个 "China" 补城市；
- 原文未披露的薪资/学历/经验保持未知；
- 详情入口、投递入口、Easy Apply、外部企业入口分开记录，永不自动提交申请。
"""
from __future__ import annotations

import hashlib
import html as html_module
import json
import re
from urllib.parse import quote_plus, urlsplit

from .models import Evidence, Job, now

CHANNEL = "linkedin"

MODES = {
    "assisted_manual": {
        "label": "人工辅助站内搜索",
        "automation": False,
        "description": "用户在本机浏览器站内搜索并打开岗位；助手只读取用户提供的原文/链接并做匹配",
        "requires_license": False,
    },
    "browser_assisted": {
        "label": "浏览器辅助检索（小样本、用户监督）",
        "automation": False,
        "description": "用用户本机已登录浏览器打开领英站内搜索页，读取可见结果并打开少量详情；"
                       "不是批量抓取，不翻页、不投递",
        "requires_license": False,
        "requires_user_confirmation": True,
    },
    "licensed_api": {
        "label": "已获平台许可的集成",
        "automation": True,
        "description": "仅在用户提供明确平台许可证据时启用；未接通前不得声称自动搜索",
        "requires_license": True,
    },
}

DEFAULT_MODE = "assisted_manual"
# 浏览器辅助检索的硬上限：一次动作最多 1 个搜索页 + 少量详情。
MAX_DETAILS = 5
DEFAULT_MAX_DETAILS = 3
MIN_PAGE_INTERVAL_SECONDS = 4.0
SEARCH_URL = "https://www.linkedin.com/jobs/search/?keywords={keywords}&location={location}"
JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}/"
OFFICIAL_RULE_URL = ("https://www.linkedin.com/help/linkedin/answer/a1341387/"
                     "prohibition-of-scraping-software?lang=en")


class LinkedInAccessError(ValueError):
    """访问模式不合法或缺少许可证据。"""


def access_profile(mode: str | None = None, license_evidence: dict | None = None) -> dict:
    """当前访问模式与能力状态；未接通自动化的模式会被如实标出来。"""
    mode = mode or DEFAULT_MODE
    if mode not in MODES:
        raise LinkedInAccessError("领英访问模式只能是 " + "、".join(MODES))
    spec = MODES[mode]
    licensed = bool(license_evidence and (license_evidence.get("evidence_url")
                                          or license_evidence.get("contract_id")))
    if spec["requires_license"] and not licensed:
        raise LinkedInAccessError(
            "licensed_api 需要明确的平台许可证据（evidence_url 或 contract_id）；"
            "没有许可时请使用 assisted_manual，不要以自动化方式访问领英")
    return {
        "channel": CHANNEL,
        "mode": mode,
        "label": spec["label"],
        "automation_enabled": bool(spec["automation"] and licensed),
        "browser_control_enabled": mode == "browser_assisted",
        "manual_path_available": True,
        "sample_limits": ({"search_pages": 1, "detail_pages": DEFAULT_MAX_DETAILS,
                           "max_detail_pages": MAX_DETAILS,
                           "min_page_interval_seconds": MIN_PAGE_INTERVAL_SECONDS}
                          if mode == "browser_assisted" else None),
        "requires_user_confirmation": bool(spec.get("requires_user_confirmation")),
        "requires_license": spec["requires_license"],
        "license_evidence": license_evidence or None,
        "policy_url": OFFICIAL_RULE_URL,
        "capabilities": {
            "open_login_in_external_browser": True,
            "verify_session": True,
            "manual_onsite_search": True,
            "read_user_provided_job_text": True,
            "auto_listing_traversal": bool(spec["automation"] and licensed),
            "auto_detail_fetch": bool(spec["automation"] and licensed),
            "browser_assisted_sample": mode == "browser_assisted",
            "bulk_listing_crawl": False,
            "job_posting_api_as_search": False,
        },
        "limits": [
            "登录成功不等于平台许可自动化访问",
            "浏览器辅助只读少量页面（1 个搜索页 + 至多 5 条详情），不翻页、不批量抓取",
            "不使用未获授权的内部接口，不把 Job Posting API 当通用搜索",
            "不自动投递，不联系招聘者；遇验证码/安全检查立即停止并交回用户",
        ],
    }


def profile_from_plan(plan: dict) -> dict:
    access = (plan or {}).get("channel_access") or {}
    row = access.get(CHANNEL) or {}
    return access_profile(row.get("mode"), row.get("license_evidence"))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def validate_material(record: dict) -> dict:
    """校验用户提供的岗位材料；缺原文或未声明有权提供都拒绝。"""
    if not isinstance(record, dict):
        raise LinkedInAccessError("岗位材料必须是对象")
    if record.get("authorized") is not True:
        raise LinkedInAccessError("用户提供的材料必须显式声明 authorized=true（本人有权提供）")
    url = str(record.get("url") or "").strip()
    if not url.startswith("http"):
        raise LinkedInAccessError("岗位材料缺少原始链接")
    host = _host(url)
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise LinkedInAccessError("领英岗位材料的链接必须是 linkedin.com 页；"
                                  "企业官网/其它平台链接请走对应渠道")
    for field in ("company", "title", "description"):
        if not str(record.get(field) or "").strip():
            raise LinkedInAccessError(f"岗位材料缺少必填字段 {field}")
    return record


def job_from_material(record: dict, *, mode: str = DEFAULT_MODE, source_ref: str | None = None) -> Job:
    """把用户提供的领英岗位材料变成一个带原文证据的 Job。"""
    record = validate_material(record)
    text = "\n".join(str(record.get(k) or "") for k in
                     ("title", "company", "description", "requirements", "location_raw"))
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    location = str(record.get("location_raw") or "").strip()
    notes = [
        "由用户本人提供的领英岗位原文；本技能未自动抓取该页面",
        "登录成功不等于平台许可自动化访问；来源按 assisted_manual 记录",
    ]
    if not location:
        notes.append("原文未给出工作地点；不从设备位置、企业所在地或单独 China 补全")
    apply_type = record.get("apply_type")
    if apply_type not in ("easy_apply", "external", "unknown", None):
        raise LinkedInAccessError("apply_type 只能是 easy_apply/external/unknown")
    job = Job(
        company=str(record["company"]).strip(),
        title=str(record["title"]).strip(),
        description=str(record["description"]).strip(),
        requirements=(str(record.get("requirements")).strip() if record.get("requirements") else None),
        source_url=record["url"],
        detail_url=record["url"],
        source_channel=CHANNEL,
        source_type="platform",
        source_id=str(record.get("job_id") or "") or None,
        location_raw=location or None,
        locations=[location] if location else [],
        education_raw=record.get("education_raw") or None,
        experience_raw=record.get("experience_raw") or None,
        salary_raw=record.get("salary_raw") or None,
        recruitment_type=record.get("recruitment_type") or None,
        posted_at=record.get("posted_at") or None,
        open_status="UNKNOWN",
        details_complete=bool(len(str(record["description"]).strip()) > 20),
        evidence_level="original_detail",
        apply_type=apply_type,
        application_url=record.get("apply_url") or None,
        application_method=record.get("apply_method") or None,
        evidence=[Evidence(source_url=record["url"],
                           retrieved_at=record.get("provided_at") or now(),
                           content_hash=content_hash,
                           method="user_provided_material",
                           snapshot=source_ref,
                           field_quotes={"title": str(record["title"]).strip(),
                                         "company": str(record["company"]).strip()})],
        notes=notes,
    )
    job.discovery_cities = []
    return job


def material_jobs(payload, *, mode: str = DEFAULT_MODE, source_ref: str | None = None):
    """批量转换用户材料；返回 (jobs, problems)。单条失败不影响其余条目。"""
    records = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not records:
        raise LinkedInAccessError("材料文件需要 jobs 数组，且不能为空")
    jobs, problems = [], []
    for index, record in enumerate(records):
        try:
            jobs.append(job_from_material(record, mode=mode, source_ref=source_ref))
        except (LinkedInAccessError, ValueError) as exc:
            problems.append({"index": index, "url": (record or {}).get("url"),
                             "reason": str(exc)})
    if not jobs:
        detail = "；".join(f"#{p['index']}: {p['reason']}" for p in problems[:3])
        raise LinkedInAccessError("没有任何可用的岗位材料：" + detail)
    return jobs, problems


def capability_summary() -> dict:
    """用于渠道菜单/能力报告展示：明确"哪些能做、哪些没接通"。"""
    profile = access_profile(DEFAULT_MODE)
    return {
        "channel": CHANNEL,
        "label": "领英",
        "access_mode": DEFAULT_MODE,
        "automation_enabled": profile["automation_enabled"],
        "manual_path_available": True,
        "message": ("默认人工辅助：本机浏览器登录 → 用户站内搜索 → 用户提供岗位原文 → 核心匹配导出。"
                    "也可用受控浏览器小样本检索（linkedin-search：1 个搜索页 + ≤5 条详情，"
                    "不翻页、不投递）；批量自动抓取未接通，需要明确的平台许可"),
        "policy_url": OFFICIAL_RULE_URL,
    }


def dump_profile(plan: dict | None = None) -> str:
    return json.dumps(profile_from_plan(plan or {}), ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ 浏览器辅助检索

class LinkedInCheckpoint(LinkedInAccessError):
    """遇到安全检查或验证码：必须停下交回用户。"""


class LinkedInLoginRequired(LinkedInAccessError):
    """未登录或登录已失效。领英会把访客重定向到访客首页，不能当成"没有岗位"。"""


_CHECKPOINT_URL = re.compile(r"/checkpoint|/challenge|/uas/|/authwall", re.I)
_CHECKPOINT_TEXT = re.compile(
    r"security check|verify you are human|unusual activity|we.?ve restricted|"
    r"请完成验证|人机验证|安全验证|账号异常|访问受限", re.I)
# 访客/登录页特征：URL 落在这些路径，或标题/文案是登录注册页。
_GUEST_URL = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/?(\?|$)|/login|/signup|/authwall", re.I)
_LOGIN_TITLE = re.compile(r"登录|注册|sign in|log in|join linkedin|authwall", re.I)
_LOGIN_TEXT = re.compile(
    r"使用邮箱登录|登录或注册|立即加入|同意并加入|agree\s*&\s*join|sign in with email|"
    r"new to linkedin|join now|forgot password", re.I)
_DESCRIPTION_START = ("about the job", "职位描述", "关于此职位", "岗位描述", "工作内容", "职位介绍", "岗位职责")
_DESCRIPTION_END = ("set alert", "设置提醒", "similar jobs", "相似职位", "meet the hiring team",
                    "招聘团队", "explore related", "相关职位", "people also viewed")
_REQUIREMENT_START = ("qualifications", "requirements", "任职资格", "任职要求", "岗位要求", "资格要求")


def search_url(keywords, location) -> str:
    """站内搜索页地址。只用公开的 jobs/search 路径，不构造内部接口。"""
    return SEARCH_URL.format(keywords=quote_plus(str(keywords).strip()),
                             location=quote_plus(str(location).strip()))


def assert_not_blocked(snapshot: dict) -> None:
    """区分三类"不能继续"：安全检查、未登录、普通页面。

    未登录时领英不会给 401，而是把 `/jobs/search` 重定向到访客首页（标题"登录或注册"）。
    这类页面必须报"需要登录"，绝不能当成"这次搜索没有岗位"。
    """
    url = str(snapshot.get("url") or "https://www.linkedin.com/")
    text = str(snapshot.get("text") or "")
    title = str(snapshot.get("title") or "")
    if _CHECKPOINT_URL.search(url) or _CHECKPOINT_TEXT.search(text):
        raise LinkedInCheckpoint(f"页面出现安全检查/验证提示（{url}）；停止自动操作，请用户处理")
    if snapshot.get("password_inputs"):
        raise LinkedInLoginRequired("页面出现密码输入框：当前会话未登录")
    if _LOGIN_TITLE.search(title) and (_LOGIN_TEXT.search(text) or _GUEST_URL.search(url)):
        raise LinkedInLoginRequired(f"当前会话未登录（页面标题：{title}）；请先登录并核验")
    if _LOGIN_TEXT.search(text) and _GUEST_URL.search(url):
        raise LinkedInLoginRequired("当前会话未登录（页面显示登录/注册入口）；请先登录并核验")


def extract_job_links(anchors, *, limit: int = DEFAULT_MAX_DETAILS) -> list[dict]:
    """从可见锚点里取岗位链接；按出现顺序去重，只保留前 limit 条。"""
    seen, out = set(), []
    for anchor in anchors or []:
        href = str((anchor or {}).get("href") or "")
        match = re.search(r"/jobs/view/(\d+)", href) or re.search(r"[?&]currentJobId=(\d+)", href)
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen:
            continue
        seen.add(job_id)
        out.append({"job_id": job_id, "url": JOB_URL.format(job_id=job_id),
                    "card_text": str((anchor or {}).get("text") or "").strip()})
        if len(out) >= limit:
            break
    return out


def _strip_html(value) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    return re.sub(r"[ \t\u00a0]+", " ", text).strip()


def _json_ld_job(snapshot: dict) -> dict | None:
    for node in snapshot.get("json_ld") or []:
        if not isinstance(node, dict):
            continue
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(str(t).lower() == "jobposting" for t in types if t):
            return node
    return None


def _json_ld_location(node: dict) -> str:
    def one(value) -> str:
        if isinstance(value, list):
            return " / ".join(filter(None, (one(v) for v in value)))
        if not isinstance(value, dict):
            return str(value or "")
        address = value.get("address") if isinstance(value.get("address"), dict) else value
        parts = [address.get("addressLocality"), address.get("addressRegion")]
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        parts.append(country)
        return "，".join(str(p).strip() for p in parts if p)

    location = node.get("jobLocation")
    if not location:
        return ""
    return one(location)


def _json_ld_salary(node: dict) -> str | None:
    salary = node.get("baseSalary")
    if isinstance(salary, dict):
        value = salary.get("value") if isinstance(salary.get("value"), dict) else salary
        low, high = value.get("minValue"), value.get("maxValue")
        unit = value.get("unitText") or ""
        currency = salary.get("currency") or value.get("currency") or ""
        if low and high:
            return f"{currency} {low}-{high} {unit}".strip()
        if low:
            return f"{currency} {low} {unit}".strip()
    return None


def parse_job_snapshot(snapshot: dict) -> dict:
    """从岗位详情页快照提取事实。优先 JSON-LD，其次可见文本；两者都不足则报错。

    不补全任何原文没有的字段：地点、薪资、学历、经验都不猜。
    """
    assert_not_blocked(snapshot)
    url = str(snapshot.get("url") or "")
    text = str(snapshot.get("text") or "")
    node = _json_ld_job(snapshot)
    facts = {"url": url, "title": "", "company": "", "location_raw": "",
             "description": "", "requirements": "", "employment_type": None,
             "posted_at": None, "salary_raw": None, "apply_type": "unknown",
             "parse_source": "visible_text"}
    if node:
        facts.update({
            "title": str(node.get("title") or "").strip(),
            "company": str((node.get("hiringOrganization") or {}).get("name") or "").strip(),
            "location_raw": _json_ld_location(node),
            "description": _strip_html(node.get("description")),
            "employment_type": (node.get("employmentType") if isinstance(node.get("employmentType"), str)
                                else ", ".join(node.get("employmentType") or []) or None),
            "posted_at": node.get("datePosted"),
            "salary_raw": _json_ld_salary(node),
            "parse_source": "json_ld",
        })

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not facts["title"]:
        headings = [h.get("text", "").strip() for h in snapshot.get("headings") or []
                    if h.get("tag") == "h1"]
        document_title = str(snapshot.get("title") or "").split(" | ")[0].strip()
        facts["title"] = next((h for h in headings if h), "") or document_title or (lines[0] if lines else "")

    if not facts["company"] or not facts["location_raw"]:
        for line in lines[:10]:
            if "·" in line:
                parts = [p.strip() for p in line.split("·") if p.strip()]
                if parts and not facts["company"]:
                    facts["company"] = parts[0]
                if len(parts) > 1 and not facts["location_raw"]:
                    candidate = parts[1]
                    if not re.fullmatch(r"\d+\s*(天|小时|周|个月|days?|hours?|weeks?)前?", candidate, re.I):
                        facts["location_raw"] = candidate
                break

    if not facts["description"]:
        start = next((i for i, line in enumerate(lines)
                      if line.strip().lower().strip("：:") in _DESCRIPTION_START), None)
        if start is not None:
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].strip().lower().strip("：:") in _DESCRIPTION_END), len(lines))
            facts["description"] = "\n".join(lines[start + 1:end]).strip()
        else:
            # 没有标准小节标题时，退化为"公司/地点行之后到页面尾部"，并标明来源。
            anchor = next((i for i, line in enumerate(lines) if "·" in line), 0)
            facts["description"] = "\n".join(lines[anchor + 1:]).strip()
            facts["parse_source"] = "visible_text_no_section"

    if not facts["requirements"]:
        req_start = next((i for i, line in enumerate(lines)
                          if line.strip().lower().strip("：:") in _REQUIREMENT_START), None)
        if req_start is not None:
            facts["requirements"] = "\n".join(lines[req_start + 1:req_start + 40]).strip()

    if re.search(r"easy\s*apply|轻松申请|一键申请", text, re.I):
        facts["apply_type"] = "easy_apply"
    elif re.search(r"company website|公司网站|外部网站", text, re.I):
        facts["apply_type"] = "external"

    job_id = re.search(r"/jobs/view/(\d+)", url)
    facts["job_id"] = job_id.group(1) if job_id else None
    missing = [name for name in ("title", "company", "description") if not facts[name]]
    if missing:
        raise LinkedInAccessError(
            "岗位页缺少可核实的原文要素：" + "、".join(missing) +
            "；不猜测内容，保留为未解析")
    return facts


def job_from_browser_snapshot(snapshot: dict, *, source_ref: str | None = None) -> Job:
    """把浏览器读到的岗位详情变成带证据的 Job（来源固定 linkedin/platform）。"""
    facts = parse_job_snapshot(snapshot)
    text = str(snapshot.get("text") or "")
    content_hash = snapshot.get("content_hash") or hashlib.sha256(text.encode()).hexdigest()
    notes = [
        "由本机独立浏览器读取的领英岗位原文（浏览器辅助，小样本）",
        "未自动投递；Easy Apply 与外部入口只记录形态，需用户本人点击",
        "平台未许可批量自动抓取；本条为受控采样，不代表站内全部岗位",
    ]
    if not facts["location_raw"]:
        notes.append("原文未给出工作地点；不从设备位置、企业所在地或单独 China 补全")
    return Job(
        company=facts["company"],
        title=facts["title"],
        description=facts["description"],
        requirements=facts["requirements"] or None,
        source_url=facts["url"],
        detail_url=facts["url"],
        source_channel=CHANNEL,
        source_type="platform",
        source_id=facts.get("job_id"),
        location_raw=facts["location_raw"] or None,
        locations=[facts["location_raw"]] if facts["location_raw"] else [],
        salary_raw=facts["salary_raw"],
        recruitment_type=facts["employment_type"],
        posted_at=facts["posted_at"],
        open_status="UNKNOWN",
        details_complete=len(facts["description"]) > 80,
        evidence_level="original_detail",
        apply_type=facts["apply_type"],
        application_url=facts["url"],
        application_method=("LinkedIn 站内 Easy Apply（需本人点击）"
                            if facts["apply_type"] == "easy_apply" else
                            "原文投递入口；未点击、未提交"),
        evidence=[Evidence(source_url=facts["url"],
                           retrieved_at=snapshot.get("retrieved_at") or now(),
                           content_hash=content_hash,
                           method="browser_visible_dom",
                           snapshot=source_ref,
                           field_quotes={"title": facts["title"], "company": facts["company"],
                                         **({"location_raw": facts["location_raw"]}
                                            if facts["location_raw"] else {})})],
        notes=notes,
    )
