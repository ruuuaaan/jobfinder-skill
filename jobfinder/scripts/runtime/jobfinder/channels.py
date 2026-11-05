"""渠道注册表：菜单、默认配置、规划、阶段映射、登录管理、导出名共用一份名单。

以前渠道名单散落在 `workflow.CHANNEL_INFO`、`workflow.SUPPORTED_CHANNELS`、
`exporter.SOURCE_LABELS`、`planning.CHANNEL_METHODS`、`service` 的硬编码元组里，
加一个渠道要改五处，漏一处就会出现"菜单里有、规划里没有"或"导出显示英文名"。
这里集中定义，其余模块只读。

字段说明
--------
`access_mode`
    - `http_listing`：服务端渲染列表页/详情页可由核心 HTTP 读取；
    - `browser_session`：必须由用户可见浏览器会话读取（登录态、反爬、动态渲染）；
    - `assisted_manual`：平台禁止未授权自动化，只能由用户自行搜索、把原文/链接交给助手。
`automation_allowed`
    `False` 表示即使登录成功也不代表平台许可自动化访问，控制器只会给出人工辅助路径。
`browser_surface`
    需要的浏览器形态：`external`（用户本机独立窗口）、`none`。
"""
from __future__ import annotations

from dataclasses import dataclass

STAGE_ORDER = ("official", "recruitment", "campus", "bendibao_web", "public_accounts")
STAGE_LABELS = {
    "official": "企业官网",
    "recruitment": "招聘平台",
    "campus": "高校就业网",
    "bendibao_web": "本地宝网站",
    "public_accounts": "公众号",
}


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    label: str
    stage: str
    note: str
    login_url: str | None = None
    access_mode: str = "http_listing"
    automation_allowed: bool = True
    browser_surface: str | None = None
    default_quota: int | None = None
    # 是否放进"可选检索渠道"菜单。人工导入与公告转载不是常规检索渠道：
    # 前者由 import 入口处理，后者由公告反查入口处理，都不能进计划任务簿。
    selectable: bool = True

    @property
    def requires_login(self) -> bool:
        return bool(self.login_url)

    @property
    def supported(self) -> bool:
        return self.selectable


CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        name="official_career", label="企业官网", stage="official",
        note="查询词/研究检索 → 企业名单 → 逐家官网核实；最慢但能拿 JD 原文",
        default_quota=10,
    ),
    ChannelSpec(
        name="public_recruitment", label="公开招聘转载", stage="official",
        note="人社/公共招聘机构的转载公告，需先审核发布方；不当作企业官网",
        selectable=False,
    ),
    ChannelSpec(
        name="51job", label="前程无忧", stage="recruitment",
        note="职能分类页直读为主，站内搜索需登录核验",
        login_url="https://login.51job.com/login.php",
        access_mode="browser_session", browser_surface="external", default_quota=5,
    ),
    ChannelSpec(
        name="zhaopin", label="智联招聘", stage="recruitment",
        note="站内搜索；未登录时薪资显示「-元」",
        login_url="https://passport.zhaopin.com/login",
        access_mode="browser_session", browser_surface="external", default_quota=5,
    ),
    ChannelSpec(
        name="boss", label="BOSS直聘", stage="recruitment",
        note="核心无授权接口，只能由宿主可见浏览器读取",
        login_url="https://www.zhipin.com/web/user/?ka=header-login",
        access_mode="browser_session", browser_surface="external", default_quota=5,
    ),
    ChannelSpec(
        name="liepin", label="猎聘", stage="recruitment",
        note="站内搜索/城市聚合页，需登录",
        login_url="https://passport.liepin.com/",
        access_mode="browser_session", browser_surface="external", default_quota=5,
    ),
    ChannelSpec(
        name="linkedin", label="领英", stage="recruitment",
        note="登录不等于平台许可自动化；默认人工辅助，另提供受控浏览器小样本检索（1 搜索页 + ≤5 详情）",
        login_url="https://www.linkedin.com/login",
        access_mode="assisted_manual", automation_allowed=False,
        browser_surface="external", default_quota=5,
    ),
    ChannelSpec(
        name="campus_job", label="高校就业网", stage="campus",
        note="学校就业网/学生就业创业服务网，校招与应届岗为主；需逐校站内检索",
        default_quota=5,
    ),
    ChannelSpec(
        name="bendibao_web", label="本地宝网站", stage="bendibao_web",
        note="偏国企/事业单位/基层岗位，需先确认站点入口",
        default_quota=5,
    ),
    ChannelSpec(
        name="bendibao_wechat", label="本地宝公众号", stage="public_accounts",
        note="拿不到后台列表，只能检索可见内容，覆盖不完整",
        access_mode="assisted_manual", default_quota=5,
    ),
    ChannelSpec(
        name="manual_import", label="人工导入", stage="none",
        note="用户有权提供的文件；不是在线检索渠道，不进入阶段任务簿",
        access_mode="assisted_manual", selectable=False,
    ),
)

BY_NAME = {c.name: c for c in CHANNELS}
# 用户可勾选的检索渠道（顺序即菜单顺序与 --all-channels 顺序）。
SUPPORTED = tuple(c.name for c in CHANNELS if c.supported)
# 需要登录交接的平台（登录 URL 是唯一判据，新增渠道只改这里）。
PLATFORM = tuple(c.name for c in CHANNELS if c.requires_login)
NON_STAGE = tuple(c.name for c in CHANNELS if c.stage == "none")


def spec(name: str) -> ChannelSpec | None:
    return BY_NAME.get(name)


def label(name: str) -> str:
    found = BY_NAME.get(name)
    return found.label if found else (name or "未披露")


def stage_of(name: str, default: str = "recruitment") -> str:
    found = BY_NAME.get(name)
    return found.stage if found and found.stage != "none" else default


def requires_login(name: str) -> bool:
    found = BY_NAME.get(name)
    return bool(found and found.requires_login)


def login_url(name: str) -> str | None:
    found = BY_NAME.get(name)
    return found.login_url if found else None


def access_mode(name: str) -> str:
    found = BY_NAME.get(name)
    return found.access_mode if found else "http_listing"


def automation_allowed(name: str) -> bool:
    found = BY_NAME.get(name)
    return bool(found.automation_allowed) if found else True


def browser_surface(name: str) -> str | None:
    found = BY_NAME.get(name)
    return found.browser_surface if found else None


def stage_channels(stage: str) -> tuple[str, ...]:
    return tuple(c.name for c in CHANNELS if c.stage == stage)


def default_quota(name: str) -> int | None:
    found = BY_NAME.get(name)
    return found.default_quota if found else None


def menu(config=None) -> list[dict]:
    """按阶段分组的渠道菜单，供 agent 原样展示给用户勾选。"""
    configured = list(getattr(config, "sources", []) or []) if config is not None else []
    rows = []
    for stage in STAGE_ORDER:
        channels = [
            {"channel": c.name, "label": c.label, "note": c.note,
             "access_mode": c.access_mode, "automation_allowed": c.automation_allowed,
             "login_required": c.requires_login, "default_quota": c.default_quota,
             "in_config": (c.name in configured) if configured else None}
            for c in CHANNELS if c.stage == stage and c.supported
        ]
        if channels:
            rows.append({"stage": stage, "label": STAGE_LABELS[stage], "channels": channels})
    return rows


def resolve_selection(channels, all_channels: bool = False) -> tuple[list[str], bool]:
    """归一化 CLI/客户端选择；必须显式给出，不能为空。"""
    if all_channels:
        return list(SUPPORTED), True
    picked = list(dict.fromkeys(channels or []))
    if not picked:
        raise ValueError(
            "必须显式选择渠道：请先展示渠道菜单，再把用户勾选结果传给 --channel（或 --all-channels）")
    unknown = [c for c in picked if c not in BY_NAME or not BY_NAME[c].supported]
    if unknown:
        raise ValueError("不支持的渠道：" + "、".join(unknown) +
                         "；可选：" + "、".join(SUPPORTED))
    return picked, False
