"""测试共用的构造器：不联网、不启动真实浏览器、不写技能目录。"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
RUNTIME = SKILL / "scripts" / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def iso(minutes_ago: float = 0) -> str:
    return (datetime.now(SHANGHAI) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def make_config(**overrides):
    from jobfinder.spec import matching_config
    data = {
        "config_kind": "user",
        "user_input_confirmed": True,
        "target_cities": ["北京"],
        "role_mode": "keywords",
        "role_keywords": ["财务BP"],
        "education": {"enabled": False},
        "sources": ["official_career", "zhaopin"],
        "budget": {"searches_per_city": 10},
    }
    data.update(overrides)
    return matching_config(data)


def make_task(task_id, *, channel="zhaopin", route="channel_search", city="北京",
              role="财务BP", status="planned", stage=None, **extra):
    from jobfinder.workflow import channel_stage
    row = {
        "task_id": task_id, "target_city": city, "role_keyword": role,
        "query": f"{channel} {city} {role}", "route": route, "channel": channel,
        "stage": stage or channel_stage(channel), "company": None, "status": status,
        "executed_at": None, "search_tool": None, "candidate_count": 0,
    }
    row.update(extra)
    return row


def make_plan(config, tasks, *, entry_tasks=None, channels=None, plan_id="plan-test",
              budget_profile=None, receipt_protocol="receipt-2"):
    from jobfinder.planning import fingerprint
    from jobfinder.models import now
    return {
        "plan_id": plan_id,
        "created_at": now(),
        "config_fingerprint": fingerprint(config),
        "protocol_version": "workflow-1.2",
        "receipt_protocol": receipt_protocol,
        "channels": channels or list(config.sources),
        "effective_config": config.model_dump(mode="json"),
        "tasks": tasks,
        "entry_tasks": entry_tasks or [],
        "budget_profile": budget_profile or {},
    }


class Workspace:
    """临时工作目录；退出时清理。"""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory(prefix="jobfinder-test-")
        self.path = Path(self._dir.name)
        (self.path / "data" / "runs").mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        self._dir.cleanup()
        return False


def start(config, plan, workspace, **kwargs):
    from jobfinder import workflow
    kwargs.setdefault("channels", list(plan["channels"]))
    kwargs.setdefault("public_only", True)
    kwargs.setdefault("user_confirmed_scope", True)
    return workflow.start_run(config, plan, workspace, **kwargs)


def search_receipt(task, *, exhausted=None, count=0, status="success", tool="test-tool",
                   executed_at=None, attempt_id=None, **extra):
    payload = {"action": "search", "task_id": task, "status": status, "search_tool": tool,
               "executed_at": executed_at or iso(), "candidate_count": count,
               "receipt_version": "receipt-2"}
    if exhausted is not None:
        payload["exhausted"] = exhausted
    if attempt_id:
        payload["attempt_id"] = attempt_id
    payload.update(extra)
    return payload


def write_snapshot(path: Path, *, text: str, url: str, anchors=None, header_text="",
                   password_inputs=0, target_id=None, session_id=None) -> Path:
    import hashlib
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload = {
        "url": url, "title": "page", "text": text, "anchors": anchors or [],
        "header_text": header_text, "password_inputs": password_inputs,
        "login_forms": 0, "has_captcha_frame": False, "target_id": target_id,
        "session_id": session_id, "retrieved_at": iso(), "content_hash": content_hash,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path
