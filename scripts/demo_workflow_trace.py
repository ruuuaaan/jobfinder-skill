"""两条受控执行轨迹：四阶段正常执行 / 某平台登录受阻后继续。

全部离线：失败证据由能力检测结果合成，登录快照是受控 DOM 样本。
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN = REPO / 'jobfinder' / 'scripts' / 'run.py'
WS = Path(sys.argv[1] if len(sys.argv) > 1 else 'trace-work')
PY = sys.executable


def cli(*args, expect=0):
    p = subprocess.run([PY, str(RUN), '--workspace', str(WS), *args],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode != expect:
        raise SystemExit(f'FAILED {args}\n{p.stdout}\n{p.stderr}')
    lines = [l for l in p.stdout.strip().splitlines() if l.strip().startswith(('{', '['))]
    return json.loads(lines[-1]) if lines else {}


def main():
    WS.mkdir(parents=True, exist_ok=True)
    subprocess.run([PY, str(RUN), '--workspace', str(WS), 'init'], check=True,
                   capture_output=True, text=True)
    config = {
        'config_kind': 'user', 'user_input_confirmed': True, 'example_only': False,
        'market_scope': 'CN_MAINLAND', 'location_mode': 'cities', 'role_mode': 'keywords',
        'role_keywords': ['财务BP'], 'target_cities': ['上海'], 'salary': {'enabled': False},
        'sources': ['official_career', '51job', 'zhaopin', 'bendibao_web', 'bendibao_wechat'],
        'budget': {'requests': None, 'searches_per_city': 40, 'companies_per_city': None,
                   'timeout': 12, 'retries': 0, 'domain_interval': 1},
        'search_provider': 'codex_assisted', 'rules_version': 'CN_MAINLAND-4.0',
        'candidate_education': None,
    }
    (WS / 'configs/trace.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    (WS / 'data' / 'discovery').mkdir(parents=True, exist_ok=True)
    (WS / 'data' / 'discovery' / 'trace-plan.json').unlink(missing_ok=True)
    cli('plan', '--config', 'configs/trace.json', '--output', 'data/discovery/trace-plan.json')

    # ---- 轨迹 A：四阶段正常执行
    started = cli('start-run', '--config', 'configs/trace.json', '--plan', 'data/discovery/trace-plan.json')
    run_a = started['run_id']
    log_a = []
    auth_dir = WS / 'data' / 'runs' / run_a / 'auth'
    auth_dir.mkdir(parents=True, exist_ok=True)
    sample = {'51job': '我的51job · 退出登录 · 个人中心', 'zhaopin': '我的简历 · 个人中心 · 退出登录'}
    for _ in range(60):
        act = cli('next-action', '--run', run_a)['action']
        step = {'action': act['action'], 'stage': act.get('stage'), 'channel': act.get('channel'),
                'task_id': act.get('task_id')}
        if act['action'] == 'open_auth_tab':
            cli('record-action', '--run', run_a, '--input', _write(run_a, 'open.json', {
                'action': 'auth_open', 'channel': act['channel'],
                'browser_context_id': 'ctx-1', 'tab_id': 'tab-1'}))
            snap = auth_dir / f"{act['channel']}.html"
            snap.write_text(f'<html><body><nav>{sample[act["channel"]]}</nav></body></html>', encoding='utf-8')
            cli('record-action', '--run', run_a, '--input', _write(run_a, 'probe.json', {
                'action': 'auth_probe', 'channel': act['channel'], 'browser_context_id': 'ctx-1',
                'tab_id': 'tab-1', 'url': {'51job': 'https://we.51job.com/pc/my/myjob',
                                           'zhaopin': 'https://www.zhaopin.com/jobs/'}[act['channel']],
                'snapshot_path': str(snap)}))
            step['action'] = 'auth_open+auth_probe'
            log_a.append(step)
            continue
        if act['action'] in ('search_task', 'read_entry'):
            cli('record-action', '--run', run_a, '--input', _write(run_a, 'search.json', {
                'action': 'search', 'task_id': act['task_id'], 'status': 'success',
                'executed_at': '2026-10-31T13:0%d:00+08:00' % (len(log_a) % 10),
                'search_tool': 'trace-tool', 'candidate_count': 0}))
            log_a.append(step)
            continue
        if act['action'] == 'close_stage':
            cli('close-stage', '--run', run_a)
            log_a.append(step)
            continue
        if act['action'] in ('finalize', 'finished'):
            cli('finalize', '--run', run_a, '--output', f'data/runs/{run_a}/finalize.json')
            completion_a = json.loads((WS / 'data/runs' / run_a / 'finalize.json').read_text(encoding='utf-8'))
            break
    else:
        raise SystemExit('trace A did not converge')

    # ---- 轨迹 B：某平台登录受阻后继续
    started = cli('start-run', '--config', 'configs/trace.json', '--plan', 'data/discovery/trace-plan.json')
    run_b = started['run_id']
    auth_dir_b = WS / 'data' / 'runs' / run_b / 'auth'
    auth_dir_b.mkdir(parents=True, exist_ok=True)
    log_b = []
    blocked_once = False
    for _ in range(60):
        act = cli('next-action', '--run', run_b)['action']
        step = {'action': act['action'], 'stage': act.get('stage'), 'channel': act.get('channel'),
                'task_id': act.get('task_id')}
        if act['action'] == 'open_auth_tab':
            channel = act['channel']
            cli('record-action', '--run', run_b, '--input', _write(run_b, 'open.json', {
                'action': 'auth_open', 'channel': channel,
                'browser_context_id': 'ctx-1', 'tab_id': 'tab-1'}))
            if channel == 'zhaopin' and not blocked_once:
                blocked_once = True
                cli('record-action', '--run', run_b, '--input', _write(run_b, 'blocked.json', {
                    'action': 'blocked', 'channel': channel,
                    'reason': '宿主浏览器扩展未连接，无法完成可见浏览器登录交接',
                    'evidence_ref': 'capability:extension-disconnected'}))
                step['action'] = 'auth_open+blocked'
                log_b.append(step)
                continue
            snap = auth_dir_b / f'{channel}-b.html'
            snap.write_text(f'<html><body><nav>{sample[channel]}</nav></body></html>', encoding='utf-8')
            cli('record-action', '--run', run_b, '--input', _write(run_b, 'probe.json', {
                'action': 'auth_probe', 'channel': channel, 'browser_context_id': 'ctx-1',
                'tab_id': 'tab-1', 'url': {'51job': 'https://we.51job.com/pc/my/myjob',
                                           'zhaopin': 'https://www.zhaopin.com/jobs/'}[channel],
                'snapshot_path': str(snap)}))
            step['action'] = 'auth_open+auth_probe'
            log_b.append(step)
            continue
        if act['action'] in ('search_task', 'read_entry'):
            cli('record-action', '--run', run_b, '--input', _write(run_b, 'search.json', {
                'action': 'search', 'task_id': act['task_id'], 'status': 'success',
                'executed_at': '2026-10-31T13:1%d:00+08:00' % (len(log_b) % 10),
                'search_tool': 'trace-tool', 'candidate_count': 0}))
            log_b.append(step)
            continue
        if act['action'] == 'close_stage':
            cli('close-stage', '--run', run_b)
            log_b.append(step)
            continue
        if act['action'] in ('finalize', 'finished'):
            cli('finalize', '--run', run_b, '--output', f'data/runs/{run_b}/finalize.json')
            completion_b = json.loads((WS / 'data/runs' / run_b / 'finalize.json').read_text(encoding='utf-8'))
            break
    else:
        raise SystemExit('trace B did not converge')

    summary = {
        'trace_a': {'run_id': run_a, 'verdict': completion_a['verdict'], 'steps': len(log_a),
                    'stages': [{s['stage']: s['status']} for s in completion_a['stages']],
                    'channels': [{c['channel']: [c['executed'], c['total'], c['auth_status']]}
                                 for c in completion_a['channels']],
                    'reasons': completion_a['reasons']},
        'trace_b': {'run_id': run_b, 'verdict': completion_b['verdict'], 'steps': len(log_b),
                    'channels': [{c['channel']: [c['executed'], c['blocked'], c['total']]}
                                 for c in completion_b['channels']],
                    'unfinished': completion_b['unfinished'],
                    'reasons': completion_b['reasons']},
    }
    out = WS / 'trace-summary.json'
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _write(run_id, name, payload):
    path = WS / 'data' / 'runs' / run_id / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return str(path)


if __name__ == '__main__':
    main()
