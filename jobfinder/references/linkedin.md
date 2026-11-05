# 领英（LinkedIn）渠道

适用版本：Skill 0.6.0 起。实现见 `jobfinder/channels.py`、`jobfinder/linkedin.py`、`jobfinder/platform_auth.py`。

## 1. 登录不等于访问许可

这是本渠道与智联/BOSS/前程无忧/猎聘最本质的差别：那四家的问题是"要登录才能看全"，
领英问题是"**即使登录，平台也不允许自动化抓取**"。官方帮助页明确禁止未经许可的抓取软件：

<https://www.linkedin.com/help/linkedin/answer/a1341387/prohibition-of-scraping-software?lang=en>

因此控制器把"账号状态"和"访问许可"分成两件事：

| 概念 | 由谁判断 | 结果 |
|---|---|---|
| 账号登录 | `auth-open` + `auth-probe`（真实浏览器 + 账号区域证据） | `auth_status=verified` 才允许继续 |
| 自动化访问许可 | 渠道注册表 `access_mode` / `licensed_api` 的许可证据 | 未获许可时只给人工辅助路径 |

`auth_status=verified` **不会**自动把 `automation_enabled` 变成 true。默认模式是
`assisted_manual`：`automation_enabled=false`，能力报告里如实写"自动化未接通"。
另有一个 `browser_assisted` 模式（见 2.1 节）：`browser_control_enabled=true`，但
`automation_enabled` 仍为 false、`bulk_listing_crawl=false`——浏览器控制不等于平台许可的自动抓取。

## 2. 默认流程（assisted_manual）

```
外部浏览器打开领英 → 用户登录 → 核验会话 → 用户在站内搜索 Jobs、设关键词与地点
→ 用户把岗位原文/链接交给助手 → linkedin-import 入库匹配 → 十列汇总 + 核查表
```

第 5 步是唯一的数据入口，命令：

```text
python -m jobfinder linkedin-import --config <配置> --input materials/linkedin.json \
  --output outputs/real/<唯一目录>
```

材料格式（`authorized=true` 是硬要求，表示用户本人有权提供）：

```json
{
  "access_mode": "assisted_manual",
  "jobs": [{
    "authorized": true,
    "url": "https://www.linkedin.com/jobs/view/4000000001",
    "job_id": "4000000001",
    "company": "示例公司",
    "title": "Finance Business Partner",
    "description": "岗位职责原文……",
    "requirements": "任职要求原文……",
    "location_raw": "Beijing, China",
    "education_raw": "Bachelor's degree preferred",
    "experience_raw": "5+ years",
    "apply_type": "easy_apply",
    "provided_at": "2026-11-02T18:00:00+08:00"
  }]
}
```

校验规则：

- `url` 必须是 `linkedin.com` 页面（企业官网/其它平台走各自渠道）；
- `company/title/description` 必填；`authorized` 必须为 `true`；
- 一条都不可用时整批报错（不返回"0 条成功"的假成功）；
- `salary_raw` / `education_raw` / `experience_raw` 缺省即未知，**不补全**；
- `location_raw` 只用原文；只写 `China` 时不补城市，地域判定落 `UNKNOWN`（进 REVIEW）。

## 2.1 受控浏览器小样本检索（`linkedin-search`）

用户不想手动复制原文时，可以用**本机已登录的浏览器**由助手代为打开检索页，读取少量页面后入库。
这条路径解决的是"没有平台许可的批量抓取，但用户可以自己点鼠标"的实际需求，因此把它做成
**受控采样**，而不是爬虫：

| 约束 | 取值 | 说明 |
|---|---|---|
| 用户同意 | 必填 `--user-confirmed` | 不传直接报错，不驱动用户浏览器 |
| 搜索页 | 1 个 | 只打开 `jobs/search?keywords=&location=`，不翻页、不改排序、不加筛选参数 |
| 详情页 | 默认 3，最多 5 | `--max-details`，超限报错；不会顺着"相似职位"继续爬 |
| 页面间隔 | 默认 ≥4 秒 | 同一平台动作间隔跨 run 记录在 `data/state/platforms.json` |
| 预算 | 该渠道配额 = 1 次搜索 + N 次 `detail_read` | 额度不足时**先停下**，不打开详情、不伪装成"没有岗位" |
| 登录前提 | 会话必须 `verified` | 传 `--run` 时强制校验；未登录会被识别为 `login_required` |
| 投递 | 绝不点击 | 只记录 Easy Apply / 外部入口形态，`auto_applied=false` |
| 并发 | 平台串行锁 | 同一平台同一时刻只允许一个执行进程 |

命令：

```text
python -m jobfinder linkedin-search --run <run_id> --config <配置> --output outputs/real/<唯一目录> \
  --keyword 财务BP [--keyword 财务分析] --location 北京 --max-details 3 --user-confirmed
```

完整顺序（登录 → 检索）：

```text
browser-status                      # 确认本机有 Chrome/Edge
auth-open --run <run_id>            # 本机独立窗口打开领英登录页
# 用户在窗口里登录
auth-probe --run <run_id> --channel linkedin   # 必须 verified
linkedin-search --run <run_id> ... --user-confirmed
```

输出与普通检索一致：`results.json`、工程核查表、十列岗位汇总（阶段性文件名）、
每页快照落在 `outputs/real/<目录>/evidence/`，证据方法 `browser_visible_dom`。

退出码：`0` 有岗位；`5` 页面读到了但没解析出岗位（含无结果）；`6` 需要用户处理
（未登录 `login_required` / 安全检查 `checkpoint`，后者会写跨 run 的封禁状态）。

**实测记录（2026-11-02，Chrome 156，headless）**：未登录直接访问 `/jobs/search` 时，
领英把页面重定向到访客首页（标题"登录或注册"、出现密码输入框）。控制器据此返回
`login_required` 并以退出码 6 结束，**没有**把它写成"0 个岗位"——这正是本版修的坑：
只认 `/login` 路径会把登录墙误判成空结果。登录后的真实岗位读取仍需用户本人完成登录后复测。

这不是"平台许可的自动抓取"：不批量、不翻页、不调内部接口、不绕验证码。用户的账号与
平台协议仍由用户自己负责；助手只做用户明确同意的少量只读操作。

## 3. 来源与事实

| 字段 | 取值 | 说明 |
|---|---|---|
| `source_channel` | `linkedin` | 不能复用官网处理器写死的 `official_career` |
| `source_type` | `platform` | 平台岗位，不是企业官网 |
| `apply_type` | `easy_apply` / `external` / `unknown` | 详情入口、投递入口、Easy Apply、外部企业入口分开记 |
| `application_url` | 原文投递入口或空 | 永不自动提交，`auto_applied=false` |
| 证据方法 | `user_provided_material` | 明确"未自动抓取该页面" |

导出时"来源渠道"显示中文"领英"（`channels.label`），三种状态列分别给出：
适配结论（SUITABLE/CONDITIONAL/NOT_SUITABLE）、证据等级、投递状态。

## 4. licensed_api（默认关闭）

只有用户能给出明确的平台许可证据才启用：

```json
{"mode": "licensed_api", "license_evidence": {"evidence_url": "https://…/contract"}}
```

缺少证据时 `access_profile()` 直接抛错，`start-run` 不会"先跑起来再说"。
即使启用，也**不把 LinkedIn Job Posting API 当通用搜索接口**，不调用未公开的内部接口；
能力报告里 `job_posting_api_as_search=false`。

## 5. 不做什么

- 不自动翻页、不自动打开岗位详情、不猜 URL、不调内部 GraphQL/REST 接口；
- 不读取/导出 Cookie、Token、账号密码；浏览器自己持久化会话；
- 不自动投递、不联系招聘者；
- 不因为"列表页可匿名读"就跳过登录或宣称自动化已接通；
- 受阻时记缺口（`blocked`/`budget_exhausted`），不写"该企业没岗位"。

## 6. 与其他渠道的一致性

领英已进入统一渠道注册表，以下位置自动一致（`tests/test_linkedin_quality.py` 校验）：

菜单与 `--all-channels`、计划任务（`stage=recruitment`、`route=channel_search`、
查询标签带"领英"）、阶段映射、登录管理（`PLATFORM_CHANNELS`、`LOGIN_URLS`）、
回执与来源、导出中文名、默认配额（5 次/城，模板 `budget.per_channel`）。

总预算与分渠道预算的换算仍按[预算](budget.md)：官网取"城市总预算 − 其它渠道合计"，
新增领英会减少官网余量，这是显式公式而不是静默挤占；预算不足时
`validate_allocation` 直接报错，要求提高预算或减少渠道。
