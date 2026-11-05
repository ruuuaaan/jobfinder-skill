"""把"条件匹配 / 证据完整度 / 投递状态"拆成三个独立结论。

旧版只有一个 `MATCH/REVIEW/EXCLUDED`：它同时表达"条件符合"和"证据不全"，
于是一个条件全 PASS、但投递入口没确认的岗位会显示成 MATCH，用户以为可以投了；
反过来，一个被 EXCLUDED 的岗位到底是因为地域、学历还是证据缺失，也看不出来。

本模块只做投影，不改变 `evaluate()` 的既有判定（旧视图继续保留）：
- `SuitabilityState`：只看条件维度（地域/职能/学历/薪资/经验/招聘类型/日期/企业范围/开放状态）；
- `EvidenceState`：原文证据等级（详情原文 / 列表或摘要 / 无）；
- `ApplicationState`：投递入口形态（明确入口 / 仅详情页 / 仅站内 Easy Apply / 未披露），
  并且永远标 `auto_applied=false`——本技能不自动投递。
"""
from __future__ import annotations

SUITABILITY_DIMENSIONS = ("geography", "role", "education", "salary", "experience",
                          "recruitment_type", "date", "company_scope", "vacancy")
EVIDENCE_DIMENSIONS = ("evidence", "details", "conflicts")
APPLICATION_LABELS = {
    "EXTERNAL_ENTRY": "外部企业投递入口（需人工提交）",
    "EASY_APPLY_ENTRY": "站内 Easy Apply 入口（需用户自行点击）",
    "DETAIL_ONLY": "仅详情页，未确认投递入口",
    "NOT_DISCLOSED": "原文未披露投递方式",
}


def suitability_state(result) -> tuple[str, list[str]]:
    """条件匹配结论。UNKNOWN 维度不会被当成符合。"""
    states = {name: dim.state for name, dim in result.dimensions.items()
              if name in SUITABILITY_DIMENSIONS}
    reasons = [f"{name}:{dim.state}" for name, dim in result.dimensions.items()
               if name in SUITABILITY_DIMENSIONS and dim.state != "PASS"]
    if "FAIL" in states.values():
        return "NOT_SUITABLE", reasons
    if not states or "UNKNOWN" in states.values():
        return "CONDITIONAL", reasons
    return "SUITABLE", reasons


def evidence_state(result) -> tuple[str, list[str]]:
    job = result.job
    level = job.evidence_level
    if level == "search_snippet":
        return "SNIPPET_ONLY", ["只有搜索摘要，未读取原文"]
    if level == "onsite_list" or not job.details_complete:
        return "LIST_ONLY", ["只读到列表/卡片，职责与要求未完整读取"]
    if result.dimensions.get("evidence") is None or result.dimensions["evidence"].state != "PASS":
        return "UNVERIFIED", ["缺少可核验的原文证据"]
    notes = []
    if not job.requirements:
        notes.append("原文未给出任职要求段落")
    return "ORIGINAL_DETAIL", notes


def application_state(job) -> tuple[str, str]:
    """投递入口形态。绝不推断"已投递"，也不构造申请地址。"""
    method = (job.application_method or "")
    url = job.application_url or ""
    if getattr(job, "apply_type", None) == "easy_apply" or "Easy Apply" in method:
        return "EASY_APPLY_ENTRY", "站内 Easy Apply；需用户本人点击提交，本技能不代为投递"
    if url:
        return "EXTERNAL_ENTRY", method or "原文给出投递入口，未验证可用性"
    if job.detail_url or job.source_url:
        return "DETAIL_ONLY", "只确认详情页入口；投递方式需用户打开原文确认"
    return "NOT_DISCLOSED", "原文未披露投递方式"


def statuses(result) -> dict:
    suitability, suitability_notes = suitability_state(result)
    evidence, evidence_notes = evidence_state(result)
    application, application_note = application_state(result.job)
    pending = list(dict.fromkeys(suitability_notes + evidence_notes + [application_note]))
    return {
        "suitability": suitability,
        "evidence": evidence,
        "application": application,
        "application_label": APPLICATION_LABELS.get(application, application),
        "auto_applied": False,
        "submission": "not_submitted",
        "pending": pending,
        # 旧视图保持兼容，便于既有报表与调用方过渡。
        "legacy_status": result.status,
    }


def summary_note(result, pending, historical=False) -> str:
    """十列汇总表的"待确认说明"：MATCH 也不能隐藏未确认事项。"""
    notes = ["历史记录（本次未重新核验）"] if historical else []
    state = statuses(result)
    if result.status == "REVIEW":
        notes.append("待确认：" + "；".join(pending or ["信息尚未完整核验"]))
    elif result.status == "MATCH":
        leftovers = [x for x in (pending or []) if x]
        if leftovers:
            notes.append("已核对条件，但仍有未确认事项：" + "；".join(leftovers))
        else:
            notes.append("条件均已核对；投递需本人确认")
    # 投递状态始终写明：MATCH 也不隐藏"投递入口未确认/需人工提交"。
    notes.append("投递入口：" + state["application_label"] + "；未自动投递")
    return "\n".join(dict.fromkeys(notes))
