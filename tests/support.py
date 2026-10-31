"""Controlled test helpers: no network, no real login, no real host browser."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'jobfinder' / 'scripts' / 'runtime'
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from jobfinder.planning import build_plan, fingerprint  # noqa: E402
from jobfinder.spec import matching_config  # noqa: E402
from jobfinder.models import now  # noqa: E402

FOUR = ['official_career', '51job', 'bendibao_web', 'bendibao_wechat']


def config_dict(channels=None, *, city='北京', role='薪酬经理', budget=40, public_only=False):
    return {
        'config_kind': 'user',
        'user_input_confirmed': True,
        'example_only': False,
        'market_scope': 'CN_MAINLAND',
        'location_mode': 'cities',
        'role_mode': 'keywords',
        'role_keywords': [role],
        'target_cities': [city],
        'salary': {'enabled': False},
        'experience': None,
        'recruitment_type': None,
        'unknown_policy': 'review',
        'sources': list(channels or FOUR),
        'budget': {'requests': None, 'searches_per_city': budget, 'companies_per_city': None,
                   'timeout': 12, 'retries': 0, 'domain_interval': 1},
        'search_provider': 'codex_assisted',
        'rules_version': 'CN_MAINLAND-4.0',
        'candidate_education': None,
    }


def make_config(**kwargs):
    return matching_config(config_dict(**kwargs))


def make_plan(config, **kwargs):
    plan = build_plan(config, **kwargs)
    plan['config_fingerprint'] = fingerprint(config)
    return plan


def workspace():
    return Path(tempfile.mkdtemp(prefix='jf-test-'))


def write_snapshot(ws, run_id, name, text):
    folder = Path(ws) / 'data' / 'runs' / run_id / 'auth'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding='utf-8')
    return path


LOGGED_IN_51JOB = """<html><body><nav>我的51job · 退出登录</nav>
<div>推荐职位 前端开发</div></body></html>"""
LOGIN_FORM_51JOB = """<html><body><h1>请登录</h1><form>手机号 密码 登录</form></body></html>"""
LOGGED_IN_ZHAOPIN = """<html><body><nav>我的简历 · 个人中心 · 退出登录</nav></body></html>"""


def auth_probe(ws, state, channel, snapshot_text, *, context='ctx-1', tab='tab-1',
               url=None, name=None):
    from jobfinder import workflow
    path = write_snapshot(ws, state['run_id'], name or f'{channel}.html', snapshot_text)
    return workflow.record_action(state, {
        'action': 'auth_probe', 'channel': channel, 'browser_context_id': context,
        'tab_id': tab, 'url': url or {'51job': 'https://we.51job.com/pc/my/myjob',
                                      'zhaopin': 'https://www.zhaopin.com/jobs/'}.get(channel),
        'snapshot_path': str(path)}, ws)


def search(ws, state, task_id, status='success', executed_at=None, count=0):
    from jobfinder import workflow
    return workflow.record_action(state, {
        'action': 'search', 'task_id': task_id, 'status': status,
        'executed_at': executed_at or now(), 'search_tool': 'test-tool',
        'candidate_count': count}, ws)


def open_auth(ws, state, channel, context='ctx-1', tab='tab-1'):
    from jobfinder import workflow
    return workflow.record_action(state, {
        'action': 'auth_open', 'channel': channel,
        'browser_context_id': context, 'tab_id': tab}, ws)


def tasks_of(state, channel=None, stage=None):
    out = [t for t in state['tasks'] if (channel is None or t['channel'] == channel)]
    return [t for t in out if stage is None or t['stage'] == stage]


def run_all_tasks(ws, state, *, status='success'):
    """按控制器给出的顺序执行当前阶段的全部可执行任务。"""
    from jobfinder import workflow
    guard = 0
    while guard < 200:
        guard += 1
        action = workflow.next_action(state)
        if action['action'] == 'open_auth_tab':
            open_auth(ws, state, action['channel'])
            auth_probe(ws, state, action['channel'],
                       LOGGED_IN_51JOB if action['channel'] == '51job' else LOGGED_IN_ZHAOPIN)
            continue
        if action['action'] == 'wait_user_auth':
            auth_probe(ws, state, action['channel'],
                       LOGGED_IN_51JOB if action['channel'] == '51job' else LOGGED_IN_ZHAOPIN)
            continue
        if action['action'] in ('search_task', 'read_entry'):
            search(ws, state, action['task_id'], status)
            continue
        if action['action'] == 'close_stage':
            workflow.close_stage(state, state['current_stage'])
            nxt = workflow.next_action(state)
            if nxt['action'] == 'close_stage':
                continue
            if nxt['action'] == 'finalize':
                state['finished'] = True
                return workflow.finalize(state)
            continue
        if action['action'] == 'finalize':
            state['finished'] = True
            return workflow.finalize(state)
        if action['action'] == 'finished':
            return workflow.finalize(state)
        return action
    raise AssertionError('controller loop did not converge')
