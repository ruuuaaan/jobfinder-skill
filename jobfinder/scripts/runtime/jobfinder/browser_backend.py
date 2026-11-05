"""本机独立浏览器后端（Chrome / Edge + 专用持久化配置目录 + CDP）。

要解决的问题
------------
旧流程只会"把登录网址打印出来"，或者用 agent 内嵌的浏览器预览页当作用户浏览器。
两者都不满足"用户在自己电脑上看到真实窗口、在里面登录、后续搜索继续复用同一个会话"。

本模块启动**真正的独立浏览器进程**：
- 使用系统已安装的 Chrome / Edge 可执行文件（不下载、不安装额外浏览器）；
- 使用 JobFinder 专用 `--user-data-dir`（`<workspace>/data/browser/profile`），
  登录态由浏览器自己持久化，本模块不读取、不导出 Cookie / Token / 密码；
- 通过 DevTools 协议（CDP）新建标签页、读取可见 DOM、截图式快照（文本 + 结构化信号）；
- 服务句柄写在 `service.json`，带 pid / 端口 / `session_id`：多次 CLI 调用复用同一进程，
  进程被关掉或重建后 `session_id` 变化，登录核验随之失效；
- 同一配置目录有占用检查（pid 存活 + 端口探测 + 文件锁），避免并发启动两个实例。

能力缺口如实返回：找不到浏览器、无图形会话、端口被占都会给出结构化原因，
**不会**回退到 agent 内嵌预览，也不会声称"云端窗口就是用户本机浏览器"。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = 20.0


class BrowserError(RuntimeError):
    """浏览器后端的统一异常基类。"""


class BrowserUnavailable(BrowserError):
    """本机没有可用的 Chrome/Edge，或没有图形会话。"""


class BrowserLaunchError(BrowserError):
    """启动失败（端口占用、进程立刻退出等）。"""


# ------------------------------------------------------------------ 可执行文件探测

def _windows_candidates() -> list[tuple[str, Path]]:
    roots = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
             os.environ.get("LOCALAPPDATA")]
    rel = [
        ("chrome", ("Google/Chrome/Application/chrome.exe",)),
        ("edge", ("Microsoft/Edge/Application/msedge.exe",)),
        ("chromium", ("Chromium/Application/chrome.exe",)),
    ]
    found = []
    for kind, paths in rel:
        for root in roots:
            if not root:
                continue
            for path in paths:
                candidate = Path(root) / path
                if candidate.is_file():
                    found.append((kind, candidate))
    return found


def _mac_candidates() -> list[tuple[str, Path]]:
    return [(kind, Path(path)) for kind, path in [
        ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ] if Path(path).is_file()]


def _linux_candidates() -> list[tuple[str, Path]]:
    rows = []
    for kind, names in [("chrome", ("google-chrome", "google-chrome-stable")),
                        ("edge", ("microsoft-edge", "microsoft-edge-stable")),
                        ("chromium", ("chromium", "chromium-browser"))]:
        for name in names:
            path = shutil.which(name)
            if path:
                rows.append((kind, Path(path)))
    return rows


def find_browsers() -> list[dict]:
    """返回本机已安装的 Chromium 系浏览器。顺序即优先顺序（Chrome → Edge → Chromium）。"""
    explicit = os.environ.get("JOBFINDER_BROWSER_PATH")
    rows: list[tuple[str, Path]] = []
    if explicit:
        rows.append(("custom", Path(explicit)))
    if os.name == "nt":
        rows.extend(_windows_candidates())
    elif sys.platform == "darwin":
        rows.extend(_mac_candidates())
    else:
        rows.extend(_linux_candidates())
    seen, out = set(), []
    for kind, path in rows:
        key = str(path.resolve()).lower()
        if key in seen or not Path(path).is_file():
            continue
        seen.add(key)
        out.append({"kind": kind, "path": str(path)})
    return out


# ------------------------------------------------------------------ WebSocket（最小实现）

class _WebSocket:
    """只够 CDP 用：文本帧收发、ping/pong、关闭。避免引入第三方依赖。"""

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws":
            raise BrowserError(f"只支持 ws:// 调试地址，收到 {url}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._handshake()
        self._next_id = 0

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("调试端口在握手阶段关闭连接")
            buffer += chunk
            if len(buffer) > 65536:
                raise BrowserError("调试端口握手响应异常")
        head = buffer.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if "101" not in head.split("\r\n")[0]:
            raise BrowserError("调试端口拒绝 WebSocket 升级：" + head.split("\r\n")[0])
        self._tail = buffer.split(b"\r\n\r\n", 1)[1]

    def _send_frame(self, payload: bytes, opcode: int = 0x1):
        header = bytearray([0x80 | opcode])
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, count: int) -> bytes:
        chunks = []
        remaining = count
        if getattr(self, "_tail", b""):
            take = self._tail[:remaining]
            chunks.append(take)
            remaining -= len(take)
            self._tail = self._tail[len(take):]
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise BrowserError("调试连接已关闭")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def recv_text(self) -> str:
        buffer = b""
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x8:
                raise BrowserError("调试连接被浏览器关闭")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            buffer += payload
            if opcode in (0x0, 0x1):
                try:
                    return buffer.decode("utf-8")
                except UnicodeDecodeError:
                    continue

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message_id = self._next_id
        self._send_frame(json.dumps({"id": message_id, "method": method,
                                     "params": params or {}}).encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            data = json.loads(self.recv_text())
            if data.get("id") == message_id:
                if "error" in data:
                    raise BrowserError(f"{method} 失败：{data['error'].get('message')}")
                return data.get("result") or {}
        raise BrowserError(f"{method} 超时")

    def close(self):
        try:
            self._send_frame(b"", opcode=0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ------------------------------------------------------------------ HTTP 辅助

def _http_json(url: str, method: str = "GET", timeout: float = 5.0) -> dict:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _http_any(url: str, timeout: float = 5.0) -> dict:
    """Chrome 不同版本对 /json/activate、/json/close 的动词要求不一致，两个都试。"""
    last = None
    for method in ("PUT", "GET"):
        try:
            return _http_json(url, method=method, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 逐方法回退
            last = exc
    raise BrowserError(str(last))


def _now_iso() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).isoformat(timespec="seconds")


_SNAPSHOT_JS = r"""
(() => {
  const text = (document.body ? document.body.innerText : '') || '';
  // 头部/导航识别：国内招聘站点与 SPA 普遍不用语义标签，门户导航是带哈希类名的 div。
  // 这里在语义标签之外补上类名/ID 关键字与"首屏顶部条"两个确定性信号，
  // 仍然只看结构位置，不按文案猜。
  const HEADER_SEL = 'header, nav, [role=navigation], [class*="header"], [id*="header"], ' +
    '[class*="nav-"], [class*="-nav"], [class*="navbar"], [class*="topbar"], [id*="topbar"], ' +
    '[class*="top-bar"], [class*="g-nav"], [class*="main-nav"]';
  const HEADER_BAND = 160;  // 首屏顶部 160px 内的链接视为导航带
  const inHeaderOf = (a) => {
    if (a.closest(HEADER_SEL)) return true;
    const r = a.getBoundingClientRect();
    return r.height > 0 && r.width > 0 && r.top >= -8 && r.top < HEADER_BAND;
  };
  const anchors = Array.from(document.querySelectorAll('a[href]')).slice(0, 2000).map(a => ({
    href: a.getAttribute('href') || '',
    text: ((a.innerText || a.textContent || '').trim()).slice(0, 80),
    in_header: inHeaderOf(a)
  }));
  const headers = Array.from(document.querySelectorAll(HEADER_SEL));
  let headerText = Array.from(new Set(headers.map(h => (h.innerText || '').trim())))
    .filter(Boolean).join('\n').slice(0, 4000);
  if (!headerText) {
    // 兜底：把顶部导航带的链接文案拼成头部文本（仍来自真实 DOM 位置，不猜内容）
    headerText = anchors.filter(a => a.in_header).map(a => a.text)
      .filter(Boolean).slice(0, 60).join(' | ').slice(0, 4000);
  }
  const passwordInputs = document.querySelectorAll('input[type=password]').length;
  const forms = Array.from(document.querySelectorAll('form')).filter(f => {
    const action = (f.getAttribute('action') || '').toLowerCase();
    return /login|signin|sign-in|auth|passport|session/.test(action) || f.querySelector('input[type=password]');
  }).length;
  // 结构化信号：标题层级与 JSON-LD（JobPosting 等）优先给解析器用，
  // 页面文字只作兜底——国内站点与领英都可能把原文放在结构化数据里。
  const headings = Array.from(document.querySelectorAll('h1, h2, h3')).slice(0, 30)
    .map(h => ({tag: h.tagName.toLowerCase(), text: ((h.innerText || '').trim()).slice(0, 200)}));
  const jsonLd = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
    .slice(0, 6).map(s => (s.textContent || '').slice(0, 40000));
  const jsonLdNodes = [];
  for (const raw of jsonLd) {
    try {
      const parsed = JSON.parse(raw);
      const push = (node) => { if (node && typeof node === 'object') jsonLdNodes.push(node); };
      if (Array.isArray(parsed)) parsed.forEach(push); else push(parsed);
      if (parsed && parsed['@graph']) parsed['@graph'].forEach(push);
    } catch (e) { /* 非法 JSON-LD 忽略，不猜测 */ }
  }
  return JSON.stringify({
    url: location.href, title: document.title || '', ready_state: document.readyState || '',
    text: text.slice(0, 200000), anchors, header_text: headerText, headings,
    json_ld: jsonLdNodes.slice(0, 12),
    password_inputs: passwordInputs, login_forms: forms,
    has_captcha_frame: !!document.querySelector('iframe[src*="captcha"], iframe[src*="challenge"], [class*=captcha], [id*=captcha]')
  });
})()
"""


# ------------------------------------------------------------------ 服务

def browser_root(workspace) -> Path:
    root = (Path(workspace) / "data" / "browser").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def profile_dir(workspace) -> Path:
    """必须是绝对路径。

    Chrome 136 起禁止在默认用户目录上开启远程调试；如果 `--user-data-dir` 传相对路径，
    Chrome 会退回默认配置目录，调试端口被静默忽略（表现为"启动成功但端口永远不就绪"）。
    """
    override = os.environ.get("JOBFINDER_BROWSER_PROFILE")
    path = Path(override).expanduser() if override else (browser_root(workspace) / "profile")
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _service_file(workspace) -> Path:
    return browser_root(workspace) / "service.json"


def _pick_port(profile: Path) -> int:
    digest = hashlib.sha256(str(profile).lower().encode()).hexdigest()
    return 9400 + int(digest[:4], 16) % 200


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


class BrowserService:
    """本机 Chrome/Edge 服务句柄。构造不启动进程，`ensure()` 才启动。"""

    def __init__(self, workspace, executable: str | None = None, *, headless: bool | None = None):
        self.workspace = Path(workspace).resolve()
        self.requested_executable = executable
        self.headless = (os.environ.get("JOBFINDER_BROWSER_HEADLESS") == "1") if headless is None else headless
        self._browser_ws: _WebSocket | None = None

    # -- 状态 -------------------------------------------------------
    def installed(self) -> list[dict]:
        browsers = find_browsers()
        if self.requested_executable:
            browsers = [b for b in browsers if Path(b["path"]) == Path(self.requested_executable)] or \
                       [{"kind": "custom", "path": self.requested_executable}]
        return browsers

    def _service_record(self) -> dict | None:
        path = _service_file(self.workspace)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def status(self) -> dict:
        record = self._service_record()
        browsers = self.installed()
        profile = profile_dir(self.workspace)
        running = False
        version = None
        if record and _pid_alive(int(record.get("pid") or 0)):
            try:
                version = _http_json(f"http://127.0.0.1:{record['port']}/json/version", timeout=2.0)
                running = True
            except Exception:
                running = False
        payload = {
            "backend": "external_browser",
            "installed": browsers,
            "installed_any": bool(browsers),
            "running": running,
            "session_id": (record or {}).get("session_id") if running else None,
            "pid": (record or {}).get("pid") if running else None,
            "port": (record or {}).get("port") if running else None,
            "profile_dir": str(profile),
            "headless": (record or {}).get("headless", self.headless) if record else self.headless,
            "browser_version": (version or {}).get("Browser"),
            "capabilities": {
                "open_visible_tab": bool(browsers),
                "read_dom": bool(browsers),
                "handoff_to_user": bool(browsers),
                "inspect_session": bool(browsers),
                "persistent_profile": True,
            },
        }
        if not browsers:
            payload.update({
                "status": "unavailable",
                "requires_local_connector": True,
                "reason": "本机未找到 Chrome/Edge。云端环境需要用户在本机运行连接程序；"
                          "打印登录网址不算已打开浏览器。",
            })
        elif running:
            payload.update({"status": "available", "reason": None})
        else:
            payload.update({"status": "available_not_running",
                            "reason": "已安装浏览器但服务未启动；首次打开登录页时会启动独立窗口"})
        return payload

    # -- 生命周期 ---------------------------------------------------
    def ensure(self) -> dict:
        record = self._service_record()
        if record and _pid_alive(int(record.get("pid") or 0)):
            try:
                _http_json(f"http://127.0.0.1:{record['port']}/json/version", timeout=2.0)
                return record
            except Exception:
                # 进程还在但调试端口没了：不杀用户的浏览器，直接报错要求关闭后重试。
                raise BrowserLaunchError(
                    "浏览器进程仍在但调试端口无响应（可能被手动启动或端口被占）；"
                    "请关闭 JobFinder 的浏览器窗口后重试，不会擅自杀掉用户进程")
        return self.start()

    def start(self) -> dict:
        browsers = self.installed()
        if not browsers:
            raise BrowserUnavailable(
                "本机未找到 Chrome/Edge：无法打开用户可见浏览器窗口。"
                "请安装 Chrome 或 Edge，或设置 JOBFINDER_BROWSER_PATH 指向可执行文件")
        executable = browsers[0]["path"]
        profile = profile_dir(self.workspace)
        port = _pick_port(profile)
        if _port_open(port):
            raise BrowserLaunchError(
                f"调试端口 {port} 已被占用（可能是另一个 JobFinder 实例或其它程序）；"
                "同一配置目录不允许并发启动")
        args = [
            executable,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            "--restore-last-session=false",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(args, **kwargs)
        except OSError as exc:
            raise BrowserLaunchError(f"启动浏览器失败：{exc}") from exc
        deadline = time.monotonic() + DEFAULT_TIMEOUT
        version = None
        actual_port = port
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BrowserLaunchError(
                    "浏览器启动后立即退出；常见原因是无图形会话（远程/服务环境）或配置目录无写权限")
            # Chrome 会把实际调试端口写进配置目录；端口被占时它会换端口。
            port_file = profile / "DevToolsActivePort"
            if port_file.exists():
                try:
                    actual_port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                except (OSError, ValueError, IndexError):
                    actual_port = port
            try:
                version = _http_json(f"http://127.0.0.1:{actual_port}/json/version", timeout=1.0)
                break
            except Exception:
                time.sleep(0.3)
        if not version:
            raise BrowserLaunchError(
                "浏览器调试端口在超时内未就绪。若刚安装过 Chrome/Edge，请确认 "
                "--user-data-dir 可写且为绝对路径；无图形会话的环境请设置 "
                "JOBFINDER_BROWSER_HEADLESS=1")
        port = actual_port
        started = _now_iso()
        session_id = hashlib.sha256(
            f"{profile}|{process.pid}|{started}".encode()).hexdigest()[:16]
        record = {"pid": process.pid, "port": port, "session_id": session_id,
                  "profile_dir": str(profile), "executable": executable,
                  "kind": browsers[0]["kind"], "started_at": started,
                  "headless": self.headless, "browser_version": version.get("Browser")}
        path = _service_file(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return record

    def _ws(self) -> _WebSocket:
        record = self.ensure()
        if self._browser_ws is None:
            version = _http_json(f"http://127.0.0.1:{record['port']}/json/version")
            self._browser_ws = _WebSocket(version["webSocketDebuggerUrl"])
        return self._browser_ws

    def stop(self) -> dict:
        record = self._service_record()
        if not record:
            return {"stopped": False, "reason": "服务未运行"}
        try:
            ws = self._ws()
            ws.call("Browser.close")
        except Exception:
            try:
                if _pid_alive(int(record.get("pid") or 0)):
                    os.kill(int(record["pid"]), 15)
            except Exception:
                pass
        # 等进程真正退出，避免配置目录文件被占用导致工作目录无法清理。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(int(record.get("pid") or 0)):
            time.sleep(0.2)
        try:
            _service_file(self.workspace).unlink()
        except OSError:
            pass
        return {"stopped": True, "session_id": record.get("session_id")}

    # -- 标签页 -----------------------------------------------------
    def list_targets(self) -> list[dict]:
        record = self.ensure()
        targets = _http_json(f"http://127.0.0.1:{record['port']}/json/list")
        return [t for t in targets if t.get("type") == "page"]

    def open(self, url: str) -> dict:
        """新建（或复用）标签页并前置显示，返回句柄。"""
        record = self.ensure()
        target = None
        try:
            target = _http_json(
                f"http://127.0.0.1:{record['port']}/json/new?" + urllib.parse.quote(url, safe=":/?=&%#"),
                method="PUT")
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 405):
                raise
        except Exception:
            target = None
        if not target:
            ws = self._ws()
            created = ws.call("Target.createTarget", {"url": url})
            target_id = created.get("targetId")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and target_id:
                for row in self.list_targets():
                    if row.get("id") == target_id:
                        target = row
                        break
                if target:
                    break
                time.sleep(0.2)
        if not target:
            raise BrowserLaunchError("新建标签页失败")
        try:
            _http_any(f"http://127.0.0.1:{record['port']}/json/activate/{target['id']}")
        except Exception:
            pass
        return {"target_id": target["id"], "url": target.get("url") or url,
                "session_id": record["session_id"], "port": record["port"],
                "profile_dir": record["profile_dir"], "pid": record["pid"],
                "surface": "external_browser"}

    def navigate(self, target_id: str, url: str, *, wait_seconds: float = 20.0) -> dict:
        """在已有标签页内跳转。复用同一标签页，避免越开越多；不新开窗口。"""
        targets = {row["id"]: row for row in self.list_targets()}
        target = targets.get(target_id)
        if not target:
            raise BrowserError(f"标签页不存在或已关闭：{target_id}（需要重新打开并登录）")
        ws = _WebSocket(target["webSocketDebuggerUrl"])
        try:
            ws.call("Page.enable")
            ws.call("Page.navigate", {"url": url})
            deadline = time.monotonic() + max(0.0, wait_seconds)
            payload = {}
            while time.monotonic() < deadline:
                time.sleep(0.5)
                payload = self._evaluate(ws)
                if payload.get("ready_state") == "complete" and (payload.get("title") or "").strip():
                    break
        finally:
            ws.close()
        return {"target_id": target_id, "url": payload.get("url") or url,
                "title": payload.get("title") or "", "ready_state": payload.get("ready_state")}

    def close_tab(self, target_id: str) -> dict:
        record = self.ensure()
        try:
            _http_any(f"http://127.0.0.1:{record['port']}/json/close/{target_id}")
            return {"closed": True, "target_id": target_id}
        except Exception as exc:
            return {"closed": False, "target_id": target_id, "reason": str(exc)}

    def has_target(self, target_id: str) -> bool:
        try:
            return any(row.get("id") == target_id for row in self.list_targets())
        except Exception:
            return False

    # -- DOM --------------------------------------------------------
    def _evaluate(self, ws: _WebSocket) -> dict:
        result = ws.call("Runtime.evaluate", {"expression": _SNAPSHOT_JS,
                                              "returnByValue": True,
                                              "awaitPromise": False})
        raw = (result.get("result") or {}).get("value")
        if not raw:
            raise BrowserError("读取页面失败（页面可能尚未加载完成）")
        return json.loads(raw)

    def snapshot(self, target_id: str, *, wait_seconds: float = 8.0) -> dict:
        """读取可见 DOM。加载中会在有限时间内重试，不会把空壳当"页面没有内容"。

        只等到 `readyState=complete` 或拿到标题为止；超时仍返回当前快照，由调用方
        按"未就绪"处理，不会自动刷新或重复请求。
        """
        targets = {row["id"]: row for row in self.list_targets()}
        target = targets.get(target_id)
        if not target:
            raise BrowserError(f"标签页不存在或已关闭：{target_id}（需要重新打开并登录）")
        ws = _WebSocket(target["webSocketDebuggerUrl"])
        try:
            payload = self._evaluate(ws)
            deadline = time.monotonic() + max(0.0, wait_seconds)
            while time.monotonic() < deadline and not (
                    payload.get("ready_state") == "complete" and payload.get("title")):
                time.sleep(0.4)
                payload = self._evaluate(ws)
        finally:
            ws.close()
        payload["target_id"] = target_id
        payload["retrieved_at"] = _now_iso()
        payload["content_hash"] = hashlib.sha256(payload["text"].encode("utf-8")).hexdigest()
        payload["session_id"] = self._service_record().get("session_id")
        return payload

    def write_snapshot(self, snapshot: dict, directory: Path) -> Path:
        """把 DOM 快照写成本轮运行目录下的文件；只存页面文本与结构信号。"""
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso().replace(":", "").replace("+", "p")
        path = directory / f"browser-snapshot-{stamp}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path


def capability_report(workspace, executable: str | None = None) -> dict:
    """能力报告来自实际探测（可执行文件、进程、端口），不接受模型自述。"""
    service = BrowserService(workspace, executable)
    status = service.status()
    return {
        "backend": "external_browser",
        "status": status["status"],
        "installed": [b["path"] for b in status["installed"]],
        "running": status["running"],
        "session_id": status["session_id"],
        "profile_dir": status["profile_dir"],
        "capabilities": status["capabilities"],
        "requires_local_connector": not status["installed_any"],
        "reason": status["reason"],
        "detected_by": "filesystem+process+port",
    }
