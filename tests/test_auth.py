"""平台登录前置：必须先核验当前会话，才允许该平台的业务搜索。"""
import unittest
from datetime import datetime, timedelta

from support import (LOGGED_IN_51JOB, LOGGED_IN_ZHAOPIN, LOGIN_FORM_51JOB, auth_probe, make_config,
                     make_plan, open_auth, search, tasks_of, workspace)

from jobfinder import workflow
from jobfinder.models import SHANGHAI


class AuthGateTest(unittest.TestCase):
    def setUp(self):
        self.ws = workspace()
        self.config = make_config(channels=['51job'])
        self.plan = make_plan(self.config)
        self.state = workflow.start_run(self.config, self.plan, self.ws)
        self.task = tasks_of(self.state, channel='51job')[0]

    def test_search_without_auth_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            search(self.ws, self.state, self.task['task_id'])
        self.assertIn('登录核验', str(ctx.exception))

    def test_next_action_is_open_tab_then_wait_user(self):
        action = workflow.next_action(self.state)
        self.assertEqual(action['action'], 'open_auth_tab')
        self.assertEqual(action['channel'], '51job')
        self.assertTrue(action['login_url'].startswith('https://login.51job.com'))
        open_auth(self.ws, self.state, '51job')
        self.assertEqual(workflow.next_action(self.state)['action'], 'wait_user_auth')

    def test_verified_probe_unblocks_search(self):
        open_auth(self.ws, self.state, '51job')
        result = auth_probe(self.ws, self.state, '51job', LOGGED_IN_51JOB)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(workflow.next_action(self.state)['action'], 'search_task')
        search(self.ws, self.state, self.task['task_id'])
        self.assertEqual(self.task['status'], 'executed')

    def test_login_page_without_account_ui_is_not_verified(self):
        open_auth(self.ws, self.state, '51job')
        result = auth_probe(self.ws, self.state, '51job', LOGIN_FORM_51JOB)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any('账号专属 UI' in p for p in result['problems']))
        with self.assertRaises(ValueError):
            search(self.ws, self.state, self.task['task_id'])

    def test_salary_digits_alone_are_not_login_evidence(self):
        open_auth(self.ws, self.state, '51job')
        # 页面里有真实薪资数字，但没有账号专属 UI，仍不能视为已登录
        result = auth_probe(self.ws, self.state, '51job',
                            '<html><body>财务BP 1.5-2.5万 上海</body></html>')
        self.assertEqual(result['status'], 'failed')

    def test_cross_platform_record_cannot_authorize_other_platform(self):
        config = make_config(channels=['51job', 'zhaopin'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, self.ws)
        open_auth(self.ws, state, '51job')
        auth_probe(self.ws, state, '51job', LOGGED_IN_51JOB)
        zhaopin_task = tasks_of(state, channel='zhaopin')[0]
        with self.assertRaises(ValueError):
            search(self.ws, state, zhaopin_task['task_id'])
        self.assertEqual(workflow.auth_status(state, 'zhaopin'), 'missing')
        self.assertEqual(workflow.auth_status(state, '51job'), 'verified')

    def test_context_or_tab_mismatch_is_rejected(self):
        open_auth(self.ws, self.state, '51job', context='ctx-A', tab='tab-A')
        result = auth_probe(self.ws, self.state, '51job', LOGGED_IN_51JOB,
                            context='ctx-B', tab='tab-A')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any('浏览器会话' in p for p in result['problems']))

    def test_expired_session_needs_new_probe(self):
        open_auth(self.ws, self.state, '51job')
        auth_probe(self.ws, self.state, '51job', LOGGED_IN_51JOB)
        record = self.state['auth']['51job']
        record['checked_at'] = (datetime.now(SHANGHAI) - timedelta(hours=5)).isoformat(timespec='seconds')
        self.assertEqual(workflow.auth_status(self.state, '51job'), 'expired')
        self.assertEqual(workflow.next_action(self.state)['action'], 'open_auth_tab')

    def test_probe_outside_run_directory_is_rejected(self):
        open_auth(self.ws, self.state, '51job')
        outside = self.ws / 'snapshot.html'
        outside.write_text(LOGGED_IN_51JOB, encoding='utf-8')
        with self.assertRaises(ValueError):
            workflow.record_action(self.state, {
                'action': 'auth_probe', 'channel': '51job', 'browser_context_id': 'ctx-1',
                'tab_id': 'tab-1', 'url': 'https://we.51job.com/pc/my/myjob',
                'snapshot_path': str(outside)}, self.ws)

    def test_public_only_mode_is_explicit(self):
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, self.ws, public_only=True)
        self.assertEqual(state['scope']['platform_auth_policy'], 'public_only')
        task = tasks_of(state, channel='51job')[0]
        search(self.ws, state, task['task_id'])  # 匿名读取是用户显式选择，不再要求登录
        self.assertEqual(task['status'], 'executed')


class BlockedPlatformTest(unittest.TestCase):
    def test_blocked_platform_does_not_stop_others(self):
        ws = workspace()
        config = make_config(channels=['51job', 'zhaopin'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws)
        workflow.record_action(state, {
            'action': 'blocked', 'channel': '51job', 'reason': '宿主未提供浏览器能力',
            'evidence_ref': 'capability:no-visible-browser'}, ws)
        open_auth(ws, state, 'zhaopin')
        auth_probe(ws, state, 'zhaopin', LOGGED_IN_ZHAOPIN)
        search(ws, state, tasks_of(state, channel='zhaopin')[0]['task_id'])
        completion = workflow.finalize(state, workspace=ws)
        self.assertEqual(completion['verdict'], 'PARTIAL')
        self.assertEqual(completion['unfinished'], [])
        channels = {r['channel']: r for r in completion['channels']}
        self.assertEqual(channels['51job']['blocked'], 1)
        self.assertEqual(channels['zhaopin']['executed'], 1)

    def test_user_skip_requires_explicit_confirmation(self):
        ws = workspace()
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws)
        with self.assertRaises(ValueError):
            workflow.record_action(state, {'action': 'skip', 'channel': '51job'}, ws)
        workflow.record_action(state, {'action': 'skip', 'channel': '51job',
                                       'user_confirmed': True, 'reason': '用户不想登录该平台'}, ws)
        completion = workflow.finalize(state, workspace=ws)
        self.assertEqual(completion['verdict'], 'PARTIAL')
        self.assertIn('用户跳过', ' '.join(completion['reasons']))


if __name__ == '__main__':
    unittest.main()


class PerTaskAuthTest(unittest.TestCase):
    def test_unauthenticated_platform_does_not_block_other_platform(self):
        ws = workspace()
        config = make_config(channels=['51job', 'zhaopin'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws)
        open_auth(ws, state, '51job')
        auth_probe(ws, state, '51job', LOGGED_IN_51JOB)
        action = workflow.next_action(state)
        # 51job 已核验，应当先执行 51job 的任务，而不是被未登录的智联挡住
        self.assertEqual(action['action'], 'search_task')
        self.assertEqual(action['channel'], '51job')
        search(ws, state, action['task_id'])
        action = workflow.next_action(state)
        self.assertEqual(action['action'], 'open_auth_tab')
        self.assertEqual(action['channel'], 'zhaopin')
