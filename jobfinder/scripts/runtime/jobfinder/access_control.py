"""访问控制与恢复：平台串行锁、分动作计量、跨 run 冷却、快照缓存。

原则
----
- **串行**：同一平台同一账号会话同一时刻只允许一个 worker 动作。锁是文件锁，
  跨进程生效，进程崩溃后按 pid 存活与 TTL 判定过期，不会永久卡死。
- **跨 run 保存**：平台的 429 冷却、明确拒绝、账号限制写在 `data/state/platforms.json`，
  重启、新建 run、换关键词或换城市都不会自动清除。只有冷却到期或用户显式重试才解除。
- **不做规避**：不代解验证码、不改指纹、不轮换账号/代理、不调用未公开接口。
  本模块只记录状态与拒绝执行，不提供"绕过"路径。
- **缓存**：键包含渠道、岗位身份、查询上下文与账号范围，避免把别的城市/关键词/账号的
  结果复用过来。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
LOCK_TTL_SECONDS = 1800
COOLDOWN_DEFAULT = 300


def _now():
    return datetime.now(SHANGHAI)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def _pid_alive(pid: int) -> bool:
    if not pid or pid < 0:
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


# ------------------------------------------------------------------ 平台串行锁

class PlatformBusy(RuntimeError):
    """同一平台已有其它 worker 在动作。"""


class PlatformLock:
    """`with platform_lock(workspace, channel) as held:` 形式的平台级互斥。"""

    def __init__(self, workspace, channel: str, *, ttl: int = LOCK_TTL_SECONDS, wait: float = 0.0):
        self.path = Path(workspace) / "data" / "locks" / f"{channel}.lock"
        self.channel = channel
        self.ttl = ttl
        self.wait = wait
        self.record: dict | None = None

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _stale(self, record: dict | None) -> bool:
        if not record:
            return True
        if not _pid_alive(int(record.get("pid") or 0)):
            return True
        try:
            held = datetime.fromisoformat(record.get("acquired_at"))
        except (TypeError, ValueError):
            return True
        return (_now() - held).total_seconds() > self.ttl

    def acquire(self) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, self.wait)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                record = self._read()
                if self._stale(record):
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise PlatformBusy(
                        f"{self.channel} 已有另一个执行进程在访问（pid={record.get('pid')}，"
                        f"自 {record.get('acquired_at')} 起）。同平台串行执行，请等待其结束。")
                time.sleep(0.2)
                continue
            self.record = {"channel": self.channel, "pid": os.getpid(),
                           "acquired_at": _iso(), "ttl_seconds": self.ttl}
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.record, handle, ensure_ascii=False)
            return self.record

    def release(self) -> None:
        record = self._read() or {}
        if int(record.get("pid") or 0) == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.record = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def platform_lock(workspace, channel: str, *, ttl: int = LOCK_TTL_SECONDS, wait: float = 0.0) -> PlatformLock:
    return PlatformLock(workspace, channel, ttl=ttl, wait=wait)


def lock_status(workspace) -> list[dict]:
    root = Path(workspace) / "data" / "locks"
    if not root.exists():
        return []
    rows = []
    for path in sorted(root.glob("*.lock")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        rows.append({"channel": path.stem, "held": not PlatformLock(workspace, path.stem)._stale(record),
                     **record})
    return rows


# ------------------------------------------------------------------ 跨 run 平台状态

def state_path(workspace) -> Path:
    return Path(workspace) / "data" / "state" / "platforms.json"


def load_platform_state(workspace) -> dict:
    path = state_path(workspace)
    if not path.exists():
        return {"schema_version": "platform-access-1", "channels": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("schema_version", "platform-access-1")
    data.setdefault("channels", {})
    return data


def save_platform_state(workspace, state: dict) -> Path:
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def channel_state(workspace, channel: str) -> dict:
    return (load_platform_state(workspace).get("channels") or {}).get(channel) or {}


def note_restricted(workspace, channel: str, *, reason: str, kind: str = "restricted",
                    evidence: str | None = None, retry_after: int | None = None,
                    run_id: str | None = None) -> dict:
    """记录 429 / 验证码 / 明确拒绝 / 账号限制。跨 run 保留。"""
    data = load_platform_state(workspace)
    row = data["channels"].setdefault(channel, {})
    now = _now()
    row.update({"last_status": kind, "reason": reason, "evidence_ref": evidence,
                "updated_at": _iso(now), "run_id": run_id})
    row.setdefault("history", []).append({"kind": kind, "reason": reason, "at": _iso(now),
                                          "run_id": run_id})
    if kind in ("rate_limited", "restricted", "captcha", "account_limited"):
        seconds = int(retry_after if retry_after is not None else COOLDOWN_DEFAULT)
        row["cooldown_until"] = _iso(now + timedelta(seconds=seconds))
        row["cooldown_seconds"] = seconds
        row["cooldown_reason"] = reason
    # 验证码/安全检查同样需要用户本人处理，不能等冷却自动重试。
    if kind in ("restricted", "account_limited", "captcha"):
        row["blocked"] = True
    save_platform_state(workspace, data)
    return row


def note_success(workspace, channel: str, *, run_id: str | None = None) -> dict:
    data = load_platform_state(workspace)
    row = data["channels"].setdefault(channel, {})
    row.update({"last_status": "ok", "updated_at": _iso(), "run_id": run_id})
    row.pop("blocked", None)
    if row.get("cooldown_until"):
        row["cooldown_until_observed"] = row.pop("cooldown_until")
    save_platform_state(workspace, data)
    return row


def clear_cooldown(workspace, channel: str, *, user_confirmed: bool = False) -> dict:
    """只有用户显式重试才解除冷却；助手不能自行"重置"。"""
    if not user_confirmed:
        raise PermissionError("解除平台冷却必须由用户明确重试（user_confirmed=true）")
    data = load_platform_state(workspace)
    row = data["channels"].setdefault(channel, {})
    row.pop("cooldown_until", None)
    row.pop("blocked", None)
    row["cleared_at"] = _iso()
    row["cleared_by"] = "user"
    save_platform_state(workspace, data)
    return row


def note_browser_action(workspace, channel: str, *, run_id: str | None = None,
                        url: str | None = None) -> dict:
    """记录一次浏览器动作时间，用于同一平台的动作间隔限制（跨 run 保留）。"""
    data = load_platform_state(workspace)
    row = data["channels"].setdefault(channel, {})
    row.update({"last_browser_action_at": _iso(), "last_browser_url": url, "run_id": run_id})
    save_platform_state(workspace, data)
    return row


def seconds_since_browser_action(workspace, channel: str) -> float | None:
    row = channel_state(workspace, channel)
    stamp = row.get("last_browser_action_at")
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return max(0.0, (_now() - moment).total_seconds())


def cooldown(workspace, channel: str) -> dict | None:
    """该平台当前是否处于冷却/封禁；返回结构化原因而不是布尔值。"""
    row = channel_state(workspace, channel)
    if not row:
        return None
    if row.get("blocked"):
        return {"state": "blocked", "reason": row.get("reason") or "平台已记录受限",
                "since": row.get("updated_at"), "evidence_ref": row.get("evidence_ref")}
    until = row.get("cooldown_until")
    if until:
        try:
            moment = datetime.fromisoformat(until)
        except (TypeError, ValueError):
            return None
        if moment > _now():
            return {"state": "cooling", "until": until, "reason": row.get("cooldown_reason"),
                    "seconds_remaining": int((moment - _now()).total_seconds())}
    return None


# ------------------------------------------------------------------ 快照缓存

def cache_key(channel: str, job_identity: str, *, query_context: dict | None = None,
              account_scope: str = "anonymous") -> str:
    payload = {"channel": channel, "job": job_identity, "query": query_context or {},
               "account": account_scope}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cache_dir(workspace) -> Path:
    return Path(workspace) / "data" / "state" / "page-cache"


def cache_get(workspace, key: str) -> dict | None:
    path = cache_dir(workspace) / (key + ".json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cache_put(workspace, key: str, payload: dict) -> Path:
    root = cache_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (key + ".json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def cache_stats(workspace) -> dict:
    root = cache_dir(workspace)
    if not root.exists():
        return {"entries": 0, "bytes": 0}
    files = list(root.glob("*.json"))
    return {"entries": len(files), "bytes": sum(f.stat().st_size for f in files)}
