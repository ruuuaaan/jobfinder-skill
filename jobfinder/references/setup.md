# 首次安装配置清单

> 第一次在本机使用本技能时，先读完本文件再开始任何真实检索。第三级（本机独立浏览器）不可用时，登录态与 JavaScript 渠道无法读取，只能跑离线样例或 `public_only` 匿名模式。

第一次在本机使用本技能，按 A → B → C 三级逐层确认。**做到哪一级，就只能宣称哪一级的能力**；未完成的级别按 `needs_help` 记录，不得写成"零结果"或"已覆盖"。

| 级别 | 能做什么 | 必须具备 |
|---|---|---|
| **A** | 离线样例、重导出、只处理用户提供的链接 | Python 3.11+ 与终端 |
| **B** | 真实检索：发现岗位、生成计划、回执入库 | A + 宿主联网搜索工具 |
| **C** | 智联/BOSS 等登录态页面、JavaScript 空壳页 | B + 本机已安装 Chrome/Edge |

只完成 A 时，**不得声称"已搜索"，也不得输出真实岗位表**（合成样例只能放 `outputs/test/`）。

## A. 运行环境（必做）

1. 确认版本：`python --version`，需 ≥ 3.11
2. 在技能目录**之外**建虚拟环境，用同一解释器安装技能根目录 `requirements.txt`
3. 建工作目录并 `init`（幂等，不覆盖已有文件）
4. 跑离线样例，确认核心导出链路可用

```text
python -m venv <ENV>
<ENV_PYTHON> -m pip install -r "<SKILL_DIR>/requirements.txt"
<ENV_PYTHON> "<SKILL_DIR>/scripts/run.py" --workspace "<WORKSPACE>" init
<ENV_PYTHON> "<SKILL_DIR>/scripts/run.py" --workspace "<WORKSPACE>" offline \
  --config configs/offline.synthetic.json \
  --candidates tests/fixtures/candidates.synthetic.json \
  --output outputs/test/first-run --allow-example
```

`ENV_PYTHON` 在 Windows 是 `<ENV>/Scripts/python.exe`，macOS/Linux 是 `<ENV>/bin/python`。看到 `outputs/test/first-run/岗位汇总.xlsx` 表示离线链路通了，**不代表发生过网络搜索**。

### 三个已知的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `pip install` 报 403 | 部分 PyPI 镜像对 pydantic 新版轮子返回 403 | 换官方源 `https://pypi.org/simple` |
| `ModuleNotFoundError: pydantic` | 用了系统 python，依赖装在虚拟环境里 | 全程用 `<ENV>/Scripts/python.exe` |
| 中文输出乱码或 `UnicodeEncodeError` | Windows 控制台默认编码非 UTF-8 | 设 `PYTHONIOENCODING=utf-8` |

## B. 联网搜索（真实检索必做）

技能自身**不联网搜索**：搜索由宿主执行，结果按回执格式交回核心再入库。

自检：能否调用一个联网搜索工具并拿到真实结果。

不能时：只跑离线样例，或处理用户直接提供的链接（记 `provided_links`，不能称"新搜索"）。

## C. 本机浏览器（登录态页面必做）

> **两条通道，宿主机优先**：如果宿主自带浏览器工具（WorkBuddy 内置浏览器，或扩展连的用户自己浏览器），
> 平台渠道直接用那条：浏览器打开 → 用户登录 → 工具输入检索 → 读可见 DOM，登录核验用 `host-open` + `host-probe`。
> 本文下面讲的专用 profile（`auth-open` / `auth-probe`）是**备用通道**，只在宿主没有浏览器工具时使用。
> 两条通道的能力差异见[可见浏览器采集](browser-collection.md)。

### 为什么需要

| 渠道 | 未配置时的实际表现 |
|---|---|
| 智联 | 未登录时薪资显示"-元"，拿不到实数，岗位只能落 REVIEW |
| BOSS | 返回 JavaScript 空壳，抓取读不到任何岗位信息 |
| 猎聘 / 前程无忧 | 公开详情通常可读，登录主要补"是否仍开放"和列表分页 |
| 领英 | 登录也不等于平台许可自动化；只支持人工辅助 + 用户提供原文 |

### 安装与最小运行

技能**不下载、不安装**浏览器，直接用系统已装的 Chrome / Edge：

1. 确认装了 Chrome 或 Edge（任意一个即可；装了多个时优先 Chrome）
2. 跑能力探测，看到 `installed` 里列出可执行文件：
   ```text
   <ENV_PYTHON> "<SKILL_DIR>/scripts/run.py" --workspace "<WORKSPACE>" browser-status
   ```
   `status=available_not_running` 表示可用但尚未启动；`unavailable` + `requires_local_connector=true`
   表示本机没有浏览器（云端环境需要用户在本机运行连接程序）。
3. 打开登录页（真实独立窗口，不是内嵌预览）：
   ```text
   <ENV_PYTHON> "<SKILL_DIR>/scripts/run.py" --workspace "<WORKSPACE>" auth-open --run <run_id> [--channel zhaopin]
   ```
4. 用户在窗口里登录，然后核验：
   ```text
   <ENV_PYTHON> "<SKILL_DIR>/scripts/run.py" --workspace "<WORKSPACE>" auth-probe --run <run_id> --channel zhaopin
   ```
   `status=verified` 才允许该平台业务搜索；`failed` 会列出具体原因（停在登录页、有密码框、没有账号区域链接等）。
5. 用完可 `browser-stop` 关闭；登录态由浏览器自己持久化在
   `<WORKSPACE>/data/browser/profile`，下一轮会复用（仍会重新核验）。

可选环境变量：

| 变量 | 用途 |
|---|---|
| `JOBFINDER_BROWSER_PATH` | 指定 Chrome/Edge 可执行文件（多版本或非标准安装时） |
| `JOBFINDER_BROWSER_PROFILE` | 换一个专用配置目录（默认 `<WORKSPACE>/data/browser/profile`） |
| `JOBFINDER_BROWSER_HEADLESS=1` | 无图形会话/CI 用；正常使用不要开，否则用户看不到登录窗口 |

常见启动失败原因：`--user-data-dir` 不可写（换目录）、端口被占（关掉旧实例或换配置目录）、
无图形会话（远程/服务环境，需要本机连接程序）。

### 每次检索开始前要做（不是只装一次）

浏览器装好后是长期可用的，**登录不是**。每轮真实检索开始前，控制器会先要求登录交接：
`next-action` 给出 `open_auth_tab` → `auth-open` 一次性打开本阶段所有待登录平台的登录页 →
用户逐个登录 → `auth-probe` 核验 → 才发第一条查询。

| 渠道 | 打开这个地址 |
|---|---|
| 智联 | `https://passport.zhaopin.com/login` |
| BOSS | `https://www.zhipin.com/web/user/?ka=header-login` |
| 猎聘 | `https://passport.liepin.com/` |
| 前程无忧 | `https://login.51job.com/login.php` |
| 领英 | `https://www.linkedin.com/login` |

顺序要求：**先开页面 → 用户登录 → 控制器核验会话 → 才开始搜索**。不要先搜一轮发现拿不到薪资再回头问登录，
那样等于白跑一轮，还会把"未登录"的结果误当成"没有岗位"。

核验**不看薪资数字**（它属于内容完整性，不证明登录），也不看"整页任意位置出现某个词"。
`auth-probe` 由核心读取当前标签页快照、自己算哈希，并要求：

1. 存在真实的打开事件（`browser_session_id` + `tab_id` + `surface=external_browser`）；
2. 会话仍是同一个（浏览器没被重开、标签页没被关）；
3. URL 不在登录/风控路径（`/login`、`/authwall`、`/checkpoint`、`/challenge`…）；
4. 页面没有密码输入框、登录表单、验证码组件；
5. 导航锚点里有该平台的**账号区域链接**（我的投递/消息/个人中心等），头部有账号视图标志文案。

登录态绑定当前浏览器进程与标签页：用户关窗、重开浏览器（`session_id` 变化）、退出或切换账号后，
原核验立即失效，下一次动作会要求重新 `auth-open` + `auth-probe`。有效会话可以跨轮次复用，
但每轮仍会重新核验，不需要用户反复退出登录。

### 配置后的自检

- `browser-status` 能列出可执行文件、`status` 不是 `unavailable`
- `auth-open` 能在桌面弹出独立窗口（不是内嵌预览、不是只打印链接）
- 用户登录后 `auth-probe` 返回 `verified`，并给出 `account_links_matched`
- 自检不通过时记录结构化原因（能力缺口/登录失败/标签页关闭），继续其它可访问渠道，不要反复重试同一动作

### 三条不可越过的线

- 不读取、不导出 Cookie 与账号凭据
- 不代填账号密码
- 遇验证码、滑块、二次验证停下交由用户处理，不绕过

### 证据等级

登录态下读到的是 `browser_visible_dom`，**不能**记成 Python HTTP 成功，也不能记成官方完整详情。浏览器动作数与快照数单列；Python HTTP 请求数写 0 或"未计量"，不得互相冒充。

## 装完自检清单

| 检查项 | 通过标准 |
|---|---|
| Python 版本 | ≥ 3.11，且与安装依赖用的是同一个解释器 |
| 核心导出 | 离线样例产出十列 `岗位汇总.xlsx` |
| 工作目录 | `init` 后 `configs/`、`data/`、`outputs/` 齐备，且不在技能目录内 |
| 配置形式 | 真实任务配置以标准 spec 形式保存，`validate-config` 通过 |
| 联网搜索 | 能调用宿主搜索工具并拿到真实结果 |
| 浏览器通道 | `browser-status` 列出 Chrome/Edge；`auth-open` 能弹出独立窗口 |
| 渠道登录 | 检索前由控制器发起开页，用户完成登录，`auth-probe` 返回 `verified`；薪资是否为实数不参与登录判定 |

## 首次真实任务的建议

先按"一个城市 + 一个岗位词 + 一个渠道"跑最小样本，确认该渠道能读通、字段能落到原文，再放量。不要首轮就开多城市多渠道，否则失败时无法定位是渠道问题、配置问题还是字段问题。

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `record-discovery` 报指纹不一致 | 配置不是标准 spec 形式，`config_fingerprint` 与计划不符 | 用计划内的 `effective_search_spec` 保存配置 |
| 核心 `new-search` 读取 0 页 | 平台详情页在官方来源校验处被拒（缺少官网来源） | 不伪造官网链接；改走宿主浏览器/网页工具读取，逐字段建证据 |
| 岗位被误判"长期驻外" | 描述被压缩改写，跨句拼接触发规则 | 描述与要求一律照抄原文，不做压缩 |
| 列表页隔天重读内容变了 | 平台卡片会轮换 | 当轮落盘 URL，不依赖昨日的列表页 |
| 同一岗位出现多条 | 企业直招与多个猎头各自挂载 | 核心按公司名+标题+URL 去重，挂载关系写进"待确认说明" |
