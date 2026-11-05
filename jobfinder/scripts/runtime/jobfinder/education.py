"""Deterministic education facts. No user qualification is inferred from a job."""
from enum import Enum
from dataclasses import dataclass
import re

RULES_VERSION = 'CN_MAINLAND-4.0'
LEGACY_RULES_VERSION = 'CN_MAINLAND-3.9'


class EducationLevel(str, Enum):
    UNKNOWN = 'UNKNOWN'
    NO_REQUIREMENT = 'NO_REQUIREMENT'
    PRIMARY = 'PRIMARY'
    MIDDLE_SCHOOL = 'MIDDLE_SCHOOL'
    HIGH_SCHOOL = 'HIGH_SCHOOL'
    ASSOCIATE = 'ASSOCIATE'
    BACHELOR = 'BACHELOR'
    MASTER = 'MASTER'
    DOCTORATE = 'DOCTORATE'


LABELS = dict(zip(EducationLevel, ['未设置','学历不限','小学','初中','高中 / 中专','大专','本科','硕士','博士']))
RANK = {k:i for i,k in enumerate(list(EducationLevel)[2:])}
CORE_LABELS = dict(zip(list(EducationLevel)[2:], ['小学','初中','高中','大专','本科','硕士','博士']))
PATTERNS = {
    EducationLevel.PRIMARY:r'小学', EducationLevel.MIDDLE_SCHOOL:r'初中',
    EducationLevel.HIGH_SCHOOL:r'高中|中专|中技|职高|high\s*school',
    EducationLevel.ASSOCIATE:r'大专|专科|associate(?:[’\']s)?',
    EducationLevel.BACHELOR:r'本科|学士|bachelor(?:[’\']s)?',
    EducationLevel.MASTER:r'硕士(?:研究生)?|master(?:[’\']s)?',
    EducationLevel.DOCTORATE:r'博士(?:研究生)?|doctorate|ph\.?d\.?',
}


@dataclass(frozen=True)
class EducationFact:
    minimum: EducationLevel
    reason: str


# 中文学位名 + 英文学位名（含所有格写法）。英文岗位原文里 "Bachelor's degree"、
# "Masters degree"、"PhD" 都是常见写法，必须与中文同等解析。
_DEGREE_ZH = r'小学|初中|高中|中专|大专|专科|本科|学士|硕士(?:研究生)?|博士(?:研究生)?'
_DEGREE_EN = (r"bachelor(?:['’]?s)?|master(?:['’]?s)?|doctor(?:ate|['’]s)?|ph\.?\s?d\.?|"
              r"associate(?:['’]s)?|high[\s-]*school|secondary[\s-]*school")
_DEGREE = f"(?:{_DEGREE_ZH}|{_DEGREE_EN})"
_DEGREE_TAIL = (r"(?:\s*(?:'s)?\s*(?:学历|学位|degree|degree\s+or\s+above|or\s+above|及以上))?")

# 优先（preferred）不是最低门槛：必须先从原文里剔除，再取最低学历。
_PREFERENCE = re.compile(
    _DEGREE + _DEGREE_TAIL +
    r"\s*(?:学历|学位)?\s*(?:优先|为佳|更佳|优先考虑|"
    r"preferred|is\s+preferred|a\s+plus|is\s+a\s+plus|desirable|an?\s+asset|preferred\s+qualification)",
    re.I)

# 等价经验：允许用经验替代学历，最低学历门槛无法确定，必须转人工确认。
_EQUIVALENT = re.compile(
    r"(?:or\s+(?:equivalent|related|relevant|similar)\s+(?:work\s+)?experience|"
    r"or\s+equivalent|equivalent\s+experience|"
    r"或\s*(?:同等|相当|相关)?\s*(?:\d+\s*年)?\s*(?:工作)?经验|"
    r"或\s*以上\s*(?:工作)?经验|可用\s*经验\s*替代)",
    re.I)

_CONDITIONAL = re.compile(
    r"放宽|优秀者|不接受|不含|非全日制除外|"
    r"exceptional\s+candidates|may\s+substitute|can\s+substitute|substitution\s+possible",
    re.I)


def normalize_education(raw):
    if not raw or re.fullmatch(r'\s*(?:未披露(?:学历)?|未说明|未知|unknown|n/?a)?\s*', raw, re.I):
        return EducationFact(EducationLevel.UNKNOWN, '学历未披露，不等于学历不限')
    text = re.sub(r'\s+', ' ', raw).strip()
    if _CONDITIONAL.search(text):
        return EducationFact(EducationLevel.UNKNOWN, '含条件性学历要求，最低门槛待确认')
    if _EQUIVALENT.search(text):
        # "Bachelor's degree or equivalent experience"：学历可用经验替代，不能按本科门槛硬判。
        return EducationFact(EducationLevel.UNKNOWN,
                             '学历与等价经验并列，最低门槛需人工确认')
    # 先剔除"优先"学位，避免把"硕士优先"解析成"必须硕士"。
    mandatory = _PREFERENCE.sub(' ', text)
    preferred_gone = mandatory != text
    mandatory = re.sub(r'\s+', ' ', mandatory).strip(' ,;，；')
    unrestricted = bool(re.search(
        r'学历不限|不限学历|无学历要求|不要求学历|no (?:degree|education) requirement|'
        r'degree not required', mandatory, re.I) or re.fullmatch(r'不限[。.!！]?', mandatory))
    levels = [k for k, p in PATTERNS.items() if re.search(p, mandatory, re.I)]
    if unrestricted:
        return (EducationFact(EducationLevel.UNKNOWN, '不限与强制学历同时出现，需确认')
                if levels else EducationFact(EducationLevel.NO_REQUIREMENT, '原文明示学历不限'))
    if not levels:
        if preferred_gone:
            return EducationFact(EducationLevel.UNKNOWN, '原文只给出优先学历，没有最低学历门槛')
        return EducationFact(EducationLevel.UNKNOWN, '只有优先条件或无法确认最低学历')
    if len(levels) > 1:
        conjunctions = re.search(r'同时|并|取得|必须|及其|and|plus|required', mandatory, re.I)
        alternatives = re.search(r'或|/|、|或者|\bor\b', mandatory, re.I)
        if conjunctions or not alternatives:
            return EducationFact(EducationLevel.UNKNOWN, '多重学历要求，不能猜测最低门槛')
    return EducationFact(min(levels, key=RANK.get), '按原文最低学历门槛规范化')


def candidate_level(value):
    if isinstance(value,EducationLevel):
        if value==EducationLevel.UNKNOWN:return None
        if value in RANK:return value
        raise ValueError('学历不限不是本人的学历等级')
    if value is None or value in ('','不限','未设置','UNKNOWN'):
        return None
    try:level=EducationLevel(str(value).upper())
    except ValueError:
        if re.search(r'以上|以下|优先|或者|或|至少|不高于|不低于',str(value)):
            raise ValueError('我的学历须填写明确等级，不能填写岗位门槛范围')
        level=normalize_education(str(value)).minimum
    if level not in RANK:
        raise ValueError('我的学历必须是明确学历等级；未设置请用 null')
    return level


def compare_candidate(raw, candidate):
    if candidate is None:
        return 'PASS','未设置我的学历，不以学历排除；岗位原文仍保留'
    fact=normalize_education(raw)
    if fact.minimum==EducationLevel.UNKNOWN:return 'UNKNOWN',fact.reason
    if fact.minimum==EducationLevel.NO_REQUIREMENT:return 'PASS',fact.reason
    ok=RANK[candidate]>=RANK[fact.minimum]
    return ('PASS' if ok else 'FAIL'),f'我的学历 {LABELS[candidate]}；岗位最低门槛 {LABELS[fact.minimum]}'
