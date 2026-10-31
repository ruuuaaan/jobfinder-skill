"""阶段顺序：招聘平台不得抢在官网之前；官网派生任务未结束不能关阶段。"""
import unittest

from support import (FOUR, LOGGED_IN_51JOB, LOGGED_IN_ZHAOPIN, auth_probe, make_config, make_plan,
                     open_auth, run_all_tasks, search, tasks_of, workspace)

from jobfinder import workflow


class StageOrderTest(unittest.TestCase):
    def setUp(self):
        self.ws = workspace()
        self.config = make_config(channels=FOUR)
        self.plan = make_plan(self.config)
        self.state = workflow.start_run(self.config, self.plan, self.ws)

    def test_default_order_is_four_stages(self):
        self.assertEqual(self.state['scope']['stage_order'],
                         ['official', 'recruitment', 'bendibao_web', 'public_accounts'])
        self.assertEqual(self.state['current_stage'], 'official')

    def test_recruitment_search_before_official_is_rejected(self):
        platform_task = tasks_of(self.state, channel='51job')[0]
        with self.assertRaises(ValueError) as ctx:
            search(self.ws, self.state, platform_task['task_id'])
        self.assertIn('跨阶段抢跑被拒绝', str(ctx.exception))
        self.assertIn('企业官网', str(ctx.exception))

    def test_derived_official_task_keeps_stage_open(self):
        official = tasks_of(self.state, stage='official')
        for task in official[:-1]:
            search(self.ws, self.state, task['task_id'])
        with self.assertRaises(ValueError) as ctx:
            workflow.close_stage(self.state, 'official', workspace=self.ws)
        self.assertIn('阶段尚未结束', str(ctx.exception))
        action = workflow.next_action(self.state)
        self.assertEqual(action['action'], 'search_task')
        self.assertEqual(action['task_id'], official[-1]['task_id'])

    def test_next_action_advances_only_after_stage_closed(self):
        completion = run_all_tasks(self.ws, self.state)
        self.assertEqual(completion['verdict'], 'COMPLETE')
        self.assertEqual([s['stage'] for s in completion['stages']],
                         ['official', 'recruitment', 'bendibao_web', 'public_accounts'])

    def test_bendibao_web_does_not_replace_public_accounts(self):
        config = make_config(channels=['bendibao_web', 'bendibao_wechat'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, self.ws)
        web = tasks_of(state, channel='bendibao_web')
        self.assertTrue(web)
        for task in web:
            search(self.ws, state, task['task_id'])
        workflow.close_stage(state, 'bendibao_web', workspace=self.ws)
        completion = workflow.finalize(state, workspace=self.ws)
        self.assertNotEqual(completion['verdict'], 'COMPLETE')
        self.assertTrue(any(u['channel'] == 'bendibao_wechat' for u in completion['unfinished']))

    def test_recruitment_only_run_cannot_complete_four_channels(self):
        config = make_config(channels=FOUR)
        plan = make_plan(config)
        state = workflow.start_run(config, plan, self.ws, channels=['51job'])
        completion = run_all_tasks(self.ws, state)
        completion = workflow.completion_with_channels(completion, config, [])
        self.assertEqual(completion['verdict'], 'PARTIAL')
        missing = {r['channel'] for r in completion['channels'] if r.get('status') == 'not_covered'}
        self.assertEqual(missing, {'official_career', 'bendibao_web', 'bendibao_wechat'})


class ChannelScopeTest(unittest.TestCase):
    def test_user_selected_channels_are_respected(self):
        config = make_config(channels=['51job', 'zhaopin'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, workspace())
        self.assertEqual(state['scope']['stage_order'], ['recruitment'])
        self.assertEqual(set(state['scope']['channels']), {'51job', 'zhaopin'})
        self.assertTrue(all(t['stage'] == 'recruitment' for t in state['tasks']))


if __name__ == '__main__':
    unittest.main()


class DerivedTaskTest(unittest.TestCase):
    def test_late_official_task_reopens_official_stage(self):
        ws = workspace()
        config = make_config(channels=['official_career', '51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws)
        # 先把官网阶段跑完并推进到招聘平台
        for task in tasks_of(state, stage='official'):
            search(ws, state, task['task_id'])
        action = workflow.next_action(state)
        self.assertEqual(state['current_stage'], 'recruitment')
        # official-plan / extend-plan 之后补进新任务
        derived = {'tasks': [{'task_id': '上海-entry-9', 'target_city': '上海', 'role_keyword': '薪酬经理',
                              'query': '新增企业 上海 薪酬经理 招聘', 'route': 'enterprise',
                              'channel': 'official_career', 'stage': 'official', 'company': '新增企业',
                              'status': 'planned'}]}
        workflow.record_plan_run(state, derived)
        action = workflow.next_action(state)
        self.assertEqual(state['current_stage'], 'official')
        self.assertEqual(action['task_id'], '上海-entry-9')
        platform_task = tasks_of(state, channel='51job')[0]
        with self.assertRaises(ValueError):
            search(ws, state, platform_task['task_id'])


class ScopeConfirmationTest(unittest.TestCase):
    def test_unconfirmed_narrow_scope_cannot_be_complete(self):
        ws = workspace()
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws)  # 未声明用户确认
        completion = run_all_tasks(ws, state)
        self.assertEqual(completion['verdict'], 'PARTIAL')
        self.assertTrue(any('范围未经用户确认' in r for r in completion['reasons']))
        self.assertEqual(state['scope']['missing_stages'],
                         ['official', 'bendibao_web', 'public_accounts'])

    def test_user_confirmed_narrow_scope_can_complete(self):
        ws = workspace()
        config = make_config(channels=['51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws, user_confirmed_scope=True)
        completion = run_all_tasks(ws, state)
        self.assertEqual(completion['verdict'], 'COMPLETE')
        self.assertTrue(any('用户明确收窄' in r for r in completion['reasons']))
