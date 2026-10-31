"""回执与完成判定：分批可入库，计划字段不可覆盖，空回执不等于完成。"""
import unittest

from support import (FOUR, LOGGED_IN_51JOB, LOGGED_IN_ZHAOPIN, auth_probe, make_config, make_plan,
                     open_auth, run_all_tasks, search, tasks_of, workspace)

from jobfinder import workflow
from jobfinder.planning import validate_receipt
from jobfinder.models import now


def receipt_for(plan, items, config):
    from jobfinder.planning import fingerprint
    return {'mode': 'agent_assisted', 'plan_id': plan['plan_id'],
            'config_fingerprint': fingerprint(config), 'search_mode': 'not_exposed',
            'coverage': 'synthetic', 'search_log': items, 'candidates': []}


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.ws = workspace()
        self.config = make_config(channels=['51job'])
        self.plan = make_plan(self.config)

    def test_receipt_cannot_override_plan_channel(self):
        task = self.plan['tasks'][0]
        receipt = receipt_for(self.plan, [{
            'task_id': task['task_id'], 'query': task['query'], 'target_city': task['target_city'],
            'role_keyword': task['role_keyword'], 'channel': 'zhaopin',
            'status': 'success', 'executed_at': now(), 'search_tool': 'x',
            'candidate_count': 0}], self.config)
        with self.assertRaises(ValueError) as ctx:
            validate_receipt(self.config, self.plan, receipt)
        self.assertIn('不能覆盖计划字段', str(ctx.exception))

    def test_merged_log_keeps_plan_values(self):
        task = self.plan['tasks'][0]
        receipt = receipt_for(self.plan, [{
            'task_id': task['task_id'], 'query': task['query'], 'target_city': task['target_city'],
            'role_keyword': task['role_keyword'], 'status': 'success', 'executed_at': now(),
            'search_tool': 'x', 'candidate_count': 0}], self.config)
        merged = validate_receipt(self.config, self.plan, receipt)
        self.assertEqual(merged[0]['channel'], '51job')
        self.assertEqual(merged[0]['stage'], 'recruitment')

    def test_partial_receipt_is_accepted_but_keeps_pending(self):
        config = make_config(channels=FOUR)
        plan = make_plan(config)
        state = workflow.start_run(config, plan, self.ws)
        done = tasks_of(state, stage='official')[0]
        search(self.ws, state, done['task_id'])
        completion = workflow.finalize(state, workspace=self.ws)
        self.assertNotEqual(completion['verdict'], 'COMPLETE')
        self.assertGreater(len(completion['unfinished']), 0)

    def test_empty_receipt_cannot_prove_completion(self):
        config = make_config(channels=FOUR)
        plan = make_plan(config)
        state = workflow.state_from_plan_logs(plan, [], config, [])
        completion = workflow.finalize(state)
        self.assertNotEqual(completion['verdict'], 'COMPLETE')
        self.assertEqual(completion['executed_tasks'], 0)

    def test_full_execution_with_zero_results_can_complete(self):
        config = make_config(channels=['51job', 'bendibao_web'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, self.ws, user_confirmed_scope=True)
        completion = run_all_tasks(self.ws, state)
        self.assertEqual(completion['verdict'], 'COMPLETE')
        self.assertTrue(all(r['executed'] == r['total'] for r in completion['channels']))

    def test_duplicate_receipt_is_idempotent(self):
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, self.ws)
        task = tasks_of(state, channel='51job')[0]
        open_auth(self.ws, state, '51job')
        auth_probe(self.ws, state, '51job', LOGGED_IN_51JOB)
        payload = {'action': 'search', 'task_id': task['task_id'], 'status': 'success',
                   'executed_at': now(), 'search_tool': 'x', 'candidate_count': 0}
        workflow.record_action(state, dict(payload), self.ws)
        used = state['budget_used']['searches']['北京']
        workflow.record_action(state, dict(payload), self.ws)
        self.assertEqual(state['budget_used']['searches']['北京'], used)
        self.assertEqual(task['attempts'], 1)

    def test_single_failure_retries_then_blocks_and_counts_budget(self):
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, self.ws)
        task = tasks_of(state, channel='51job')[0]
        open_auth(self.ws, state, '51job')
        auth_probe(self.ws, state, '51job', LOGGED_IN_51JOB)
        first = '2026-10-31T12:30:00+08:00'
        second = '2026-10-31T12:31:00+08:00'
        search(self.ws, state, task['task_id'], status='failed', executed_at=first)
        self.assertEqual(task['status'], 'failed')
        action = workflow.next_action(state)
        self.assertEqual(action['action'], 'search_task')
        self.assertEqual(action['task_id'], task['task_id'])
        search(self.ws, state, task['task_id'], status='failed', executed_at=second)
        self.assertEqual(task['status'], 'blocked')
        self.assertEqual(state['budget_used']['searches']['北京'], 2)
        completion = workflow.finalize(state, workspace=self.ws)
        self.assertEqual(completion['verdict'], 'PARTIAL')


class MissingChannelTest(unittest.TestCase):
    def test_channel_report_without_url_is_included(self):
        config = make_config(channels=FOUR)
        plan = make_plan(make_config(channels=['51job']))
        state = workflow.state_from_plan_logs(plan, [], config, [])
        reports = [{'channel': 'bendibao_web', 'url': None, 'access_status': 'not_run',
                    'parse_status': 'not_run', 'reason': '本轮未取得逐站点自动访问依据'}]
        completion = workflow.finalize(state)
        completion = workflow.completion_with_channels(completion, config, reports)
        row = next(r for r in completion['channels'] if r['channel'] == 'bendibao_web')
        self.assertIn('未取得逐站点自动访问依据', row['reason'])
        self.assertEqual(row['status'], 'not_covered')
        self.assertNotEqual(completion['verdict'], 'COMPLETE')

    def test_browser_collection_cannot_bypass_run_completion(self):
        from jobfinder.browser import browser_completion
        config = make_config(channels=FOUR)
        reports = [{'company': '示例公司', 'channel': 'official_career', 'url': 'https://example.invalid/jobs',
                    'access_status': 'success', 'parse_status': 'success', 'jobs_extracted': 1,
                    'coverage_complete': True, 'reason': ''}]
        completion = browser_completion(config, reports, {'plan': {'plan_id': 'p-1'}})
        self.assertEqual(completion['verdict'], 'PARTIAL')
        self.assertTrue(any(r.get('status') == 'not_covered' for r in completion['channels']))


if __name__ == '__main__':
    unittest.main()
