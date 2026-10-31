"""兼容与持久化：旧计划、旧结果、运行恢复。"""
import json
import tempfile
import unittest
from pathlib import Path

from support import make_config, make_plan, workspace

from jobfinder import workflow
from jobfinder.exporter import export_bundle
from jobfinder.exporter import SUMMARY_HEADERS


class LegacyPlanTest(unittest.TestCase):
    def test_plan_without_stage_fields_still_works(self):
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        legacy = json.loads(json.dumps(plan))
        for task in legacy['tasks']:
            task.pop('stage', None)
        legacy.pop('stage_order', None)
        legacy.pop('protocol_version', None)
        state = workflow.start_run(config, legacy, workspace())
        self.assertEqual(state['scope']['stage_order'], ['recruitment'])
        self.assertEqual(state['tasks'][0]['stage'], 'recruitment')
        self.assertEqual(state['protocol_version'], workflow.PROTOCOL_VERSION)

    def test_state_round_trip_keeps_tasks_and_budget(self):
        ws = workspace()
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws)
        workflow.next_action(state)  # open_auth_tab
        workflow.record_action(state, {'action': 'blocked', 'channel': '51job',
                                       'reason': '能力不足', 'evidence_ref': 'cap:1'}, ws)
        workflow.close_stage(state, 'recruitment', workspace=ws)
        reloaded = workflow.load_state(ws, state['run_id'])
        self.assertEqual(reloaded['run_id'], state['run_id'])
        self.assertEqual(len(reloaded['tasks']), len(state['tasks']))
        self.assertEqual(reloaded['tasks'][0]['status'], 'blocked')
        self.assertEqual(reloaded['stages']['recruitment']['status'], 'closed_with_gaps')
        self.assertEqual(reloaded['budget_used'], state['budget_used'])
        self.assertEqual(workflow.latest_run_id(ws), state['run_id'])

    def test_completion_written_to_disk(self):
        ws = workspace()
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws)
        workflow.record_action(state, {'action': 'blocked', 'channel': '51job',
                                       'reason': '能力不足', 'evidence_ref': 'cap:1'}, ws)
        completion = workflow.finalize(state, workspace=ws)
        path = Path(ws) / 'data' / 'runs' / state['run_id'] / 'completion.json'
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['verdict'], completion['verdict'])


class ExportCompatTest(unittest.TestCase):
    def test_export_without_completion_keeps_ten_columns(self):
        config = make_config(channels=['51job'])
        output = Path(tempfile.mkdtemp(prefix='jf-export-')) / 'out'
        payload = export_bundle(output, config, [], [], [], [], {'provider': 'legacy'}, events=[])
        from openpyxl import load_workbook
        book = load_workbook(output / '岗位汇总.xlsx')
        headers = [c.value for c in next(book['岗位汇总'].iter_rows(min_row=1, max_row=1))]
        self.assertEqual(tuple(headers), tuple(SUMMARY_HEADERS))
        self.assertNotIn('运行状态与完成度', book.sheetnames)
        self.assertEqual(payload['counts']['MATCH'], 0)

    def test_export_with_completion_adds_status_sheet(self):
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, workspace())
        completion = workflow.finalize(state)
        output = Path(tempfile.mkdtemp(prefix='jf-export-')) / 'out'
        export_bundle(output, config, [], [], [], [], {'provider': 'x', 'completion': completion})
        from openpyxl import load_workbook
        book = load_workbook(output / 'results.xlsx')
        self.assertIn('运行状态与完成度', book.sheetnames)


if __name__ == '__main__':
    unittest.main()
