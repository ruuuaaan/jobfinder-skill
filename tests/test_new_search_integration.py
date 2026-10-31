"""端到端：new-search 必须把回执并入运行并给出完成度，而不是只报"命令成功"。"""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import ROOT

RUN = ROOT / 'jobfinder' / 'scripts' / 'run.py'
CONFIG = {
    'config_kind': 'user', 'user_input_confirmed': True, 'example_only': False,
    'market_scope': 'CN_MAINLAND', 'location_mode': 'cities', 'role_mode': 'keywords',
    'role_keywords': ['薪酬经理'], 'target_cities': ['上海'], 'salary': {'enabled': False},
    'sources': ['official_career'],
    'budget': {'requests': None, 'searches_per_city': 40, 'companies_per_city': None,
               'timeout': 12, 'retries': 0, 'domain_interval': 1},
    'search_provider': 'codex_assisted', 'rules_version': 'CN_MAINLAND-4.0',
    'candidate_education': None,
}


class NewSearchIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix='jf-int-'))
        self.cli('init')
        (self.ws / 'configs').mkdir(exist_ok=True)
        (self.ws / 'configs/int.json').write_text(json.dumps(CONFIG, ensure_ascii=False), encoding='utf-8')
        self.cli('plan', '--config', 'configs/int.json', '--output', 'data/discovery/plan.json')
        self.plan = json.loads((self.ws / 'data/discovery/plan.json').read_text(encoding='utf-8'))

    def cli(self, *args):
        p = subprocess.run([sys.executable, str(RUN), '--workspace', str(self.ws), *args],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(p.returncode, 0, msg=p.stdout[-1500:] + p.stderr[-1500:])
        lines = [x for x in p.stdout.splitlines() if x.startswith('{')]
        return json.loads(lines[-1]) if lines else {}

    def test_start_run_requires_explicit_channel_choice(self):
        p = subprocess.run([sys.executable, str(RUN), '--workspace', str(self.ws), 'start-run',
                            '--config', 'configs/int.json', '--plan', 'data/discovery/plan.json'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(p.returncode, 2)
        self.assertIn('必须显式选择渠道', p.stderr)

    def test_channel_menu_lists_all_supported_channels(self):
        menu = self.cli('channels')
        flat = [c['channel'] for row in menu['menu'] for c in row['channels']]
        self.assertIn('boss', flat)
        self.assertIn('liepin', flat)
        self.assertIn('bendibao_wechat', flat)

    def test_new_search_reports_completion_and_keeps_ten_columns(self):
        started = self.cli('start-run', '--config', 'configs/int.json', '--plan', 'data/discovery/plan.json',
                           '--channel', 'official_career')
        run_id = started['run_id']
        now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        tasks = self.plan['tasks']
        receipt = {
            'mode': 'agent_assisted', 'plan_id': self.plan['plan_id'],
            'config_fingerprint': self.plan['config_fingerprint'], 'search_mode': 'not_exposed',
            'coverage': 'integration', 'search_log': [
                {'task_id': tasks[0]['task_id'], 'query': tasks[0]['query'], 'target_city': '上海',
                 'role_keyword': '薪酬经理', 'executed_at': now, 'search_tool': 'test-tool',
                 'status': 'success', 'candidate_count': 1},
                {'task_id': tasks[1]['task_id'], 'query': tasks[1]['query'], 'target_city': '上海',
                 'role_keyword': '薪酬经理', 'executed_at': now, 'search_tool': 'test-tool',
                 'status': 'success', 'candidate_count': 0}],
            'candidates': [{
                'query': tasks[0]['query'], 'url': 'https://example.invalid/jobs', 'title': '合成企业',
                'discovered_at': now, 'source_channel': 'official_career', 'source_type': 'official_website',
                'candidate_type': 'career_entry', 'target_cities': ['上海'],
                'task_ids': [tasks[0]['task_id']], 'company': '合成企业',
                'local_file': 'tests/fixtures/jobs.synthetic.json', 'authorized_import': True}],
        }
        (self.ws / 'data/discovery/receipt.json').write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        out = self.cli('new-search', '--config', 'configs/int.json', '--plan', 'data/discovery/plan.json',
                       '--candidates', 'data/discovery/receipt.json', '--run', run_id,
                       '--output', 'outputs/real/int-1')
        self.assertEqual(out['run_id'], run_id)
        self.assertIn(out['verdict'], ('COMPLETE', 'PARTIAL', 'WAITING_USER', 'IN_PROGRESS'))
        payload = json.loads((self.ws / 'outputs/real/int-1/results.json').read_text(encoding='utf-8'))
        completion = payload['discovery']['completion']
        self.assertEqual(completion['run_id'], run_id)
        self.assertEqual(completion['total_tasks'], len(tasks))
        from openpyxl import load_workbook
        book = load_workbook(self.ws / 'outputs/real/int-1/岗位汇总.xlsx')
        headers = [c.value for c in next(book['岗位汇总'].iter_rows(min_row=1, max_row=1))]
        self.assertEqual(len(headers), 10)
        self.assertIn('运行状态与完成度', load_workbook(self.ws / 'outputs/real/int-1/results.xlsx').sheetnames)


if __name__ == '__main__':
    unittest.main()
