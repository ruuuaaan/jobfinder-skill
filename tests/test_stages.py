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


class PersistAdvanceTest(unittest.TestCase):
    def test_stage_advance_is_persisted_after_last_task(self):
        ws = workspace()
        config = make_config(channels=['official_career', '51job'])
        plan = make_plan(config)
        state = workflow.start_run(config, plan, ws)
        for task in tasks_of(state, stage='official'):
            search(ws, state, task['task_id'])
        reloaded = workflow.load_state(ws, state['run_id'])
        self.assertEqual(reloaded['current_stage'], 'recruitment')
        self.assertEqual(reloaded['stages']['official']['status'], 'closed')


class ExhaustionGateTest(unittest.TestCase):
    """渠道必须跑到底、线索必须逐家核实——这两条由程序拦，不靠提示词。"""

    def test_missing_exhausted_is_rejected(self):
        ws = workspace()
        state = workflow.start_run(make_config(channels=['51job']), make_plan(make_config(channels=['51job'])), ws)
        task = tasks_of(state, channel='51job')[0]
        open_auth(ws, state, '51job')
        auth_probe(ws, state, '51job', LOGGED_IN_51JOB)
        with self.assertRaises(ValueError) as ctx:
            workflow.record_action(state, {'action': 'search', 'task_id': task['task_id'],
                                           'status': 'success',
                                           'executed_at': '2026-10-31T15:00:00+08:00',
                                           'candidate_count': 0}, ws)
        self.assertIn('exhausted', str(ctx.exception))

    def test_unexhausted_channel_with_budget_cannot_close_stage(self):
        ws = workspace()
        config = make_config(channels=['51job'], budget=5)
        state = workflow.start_run(config, make_plan(config), ws)
        task = tasks_of(state, channel='51job')[0]
        open_auth(ws, state, '51job')
        auth_probe(ws, state, '51job', LOGGED_IN_51JOB)
        search(ws, state, task['task_id'], exhausted=False)
        action = workflow.next_action(state)
        self.assertEqual(action['action'], 'continue_channel')
        self.assertEqual(action['budget_remaining'], 4)
        with self.assertRaises(ValueError) as ctx:
            workflow.close_stage(state, 'recruitment')
        self.assertIn('未声明检索到底', str(ctx.exception))

    def test_page_derivation_takes_over_exhaustion_duty(self):
        ws = workspace()
        config = make_config(channels=['51job'], budget=5)
        state = workflow.start_run(config, make_plan(config), ws)
        first = tasks_of(state, channel='51job')[0]
        open_auth(ws, state, '51job')
        auth_probe(ws, state, '51job', LOGGED_IN_51JOB)
        search(ws, state, first['task_id'], exhausted=False)
        workflow.record_action(state, {'action': 'register_derived', 'task_id': first['task_id'],
                                       'kind': 'page', 'items': [{'url': 'https://x/p2/', 'page': 2}]}, ws)
        self.assertTrue(first.get('continued'))
        action = workflow.next_action(state)
        self.assertEqual(action['action'], 'search_task')
        self.assertEqual(action['task_id'], first['task_id'] + '-p1')
        search(ws, state, action['task_id'], exhausted=True)
        self.assertIn(workflow.next_action(state)['action'], ('close_stage', 'finalize', 'finished'))

    def test_budget_exhausted_is_a_gap_not_complete(self):
        ws = workspace()
        config = make_config(channels=['51job'], budget=1)
        state = workflow.start_run(config, make_plan(config), ws, user_confirmed_scope=True)
        task = tasks_of(state, channel='51job')[0]
        open_auth(ws, state, '51job')
        auth_probe(ws, state, '51job', LOGGED_IN_51JOB)
        search(ws, state, task['task_id'], exhausted=False)
        workflow.close_stage(state, 'recruitment')
        self.assertEqual(task['status'], 'budget_exhausted')
        self.assertEqual(state['stages']['recruitment']['status'], 'closed_with_gaps')

    def test_discovery_leads_must_become_verify_tasks(self):
        ws = workspace()
        config = make_config(channels=['official_career'])
        state = workflow.start_run(config, make_plan(config), ws)
        research = next(t for t in state['tasks'] if t['route'] == 'research')
        with self.assertRaises(ValueError) as ctx:
            workflow.record_action(state, {'action': 'search', 'task_id': research['task_id'],
                                           'status': 'success',
                                           'executed_at': '2026-10-31T15:00:00+08:00',
                                           'candidate_count': 2, 'exhausted': True}, ws)
        self.assertIn('identified', str(ctx.exception))

    def test_identified_list_is_the_denominator(self):
        ws = workspace()
        config = make_config(channels=['official_career'])
        state = workflow.start_run(config, make_plan(config), ws)
        research = next(t for t in state['tasks'] if t['route'] == 'research')
        # 只登记 1 家，但识别清单里有 3 家 → 按 3 家核算，拒收
        with self.assertRaises(ValueError) as ctx:
            workflow.record_action(state, {'action': 'search', 'task_id': research['task_id'],
                                           'status': 'success',
                                           'executed_at': '2026-10-31T15:01:00+08:00',
                                           'candidate_count': 1, 'exhausted': True,
                                           'identified': ['甲', '乙', '丙'],
                                           'derived': [{'company': '甲'}]}, ws)
        self.assertIn('核实任务', str(ctx.exception))

    def test_derived_verify_tasks_block_official_stage_until_done(self):
        ws = workspace()
        config = make_config(channels=['official_career'])
        state = workflow.start_run(config, make_plan(config), ws)
        direct = next(t for t in state['tasks'] if t['route'] == 'direct')
        research = next(t for t in state['tasks'] if t['route'] == 'research')
        search(ws, state, direct['task_id'], count=2, identified=['甲企业', '乙企业'],
               derived=[{'company': '甲企业'}, {'company': '乙企业'}])
        self.assertEqual(len(direct['derived_ids']), 2)
        search(ws, state, research['task_id'], count=0)
        action = workflow.next_action(state)
        self.assertIn(action['action'], ('read_entry', 'search_task'))
        self.assertEqual(action['task_id'], direct['derived_ids'][0])
        self.assertEqual(action['stage'], 'official')
        with self.assertRaises(ValueError):
            workflow.close_stage(state, 'official')
        for tid in direct['derived_ids']:
            search(ws, state, tid, exhausted=None)
        self.assertIn(workflow.next_action(state)['action'], ('close_stage', 'finalize', 'finished'))


class QueuedPageTest(unittest.TestCase):
    def test_queued_task_satisfies_exhaustion_duty(self):
        """同渠道还有排队任务时，不该因为当前任务没到底就去派生新任务。"""
        ws = workspace()
        config = make_config(channels=['official_career'], budget=5)
        state = workflow.start_run(config, make_plan(config), ws)
        direct = next(t for t in state['tasks'] if t['route'] == 'direct')
        research = next(t for t in state['tasks'] if t['route'] == 'research')
        search(ws, state, direct['task_id'], exhausted=False)
        action = workflow.next_action(state)
        self.assertEqual(action['action'], 'search_task')
        self.assertEqual(action['task_id'], research['task_id'])


class OfficialVerifyUntilBudgetTest(unittest.TestCase):
    """有官网链接的企业必须逐家核实，直到官网预算用完。"""

    def test_company_with_url_cannot_be_waived(self):
        ws = workspace()
        config = make_config(channels=['official_career'])
        state = workflow.start_run(config, make_plan(config), ws)
        research = next(t for t in state['tasks'] if t['route'] == 'research')
        with self.assertRaises(ValueError) as ctx:
            workflow.record_action(state, {
                'action': 'search', 'task_id': research['task_id'], 'status': 'success',
                'executed_at': '2026-10-31T16:00:00+08:00', 'exhausted': True,
                'identified': [{'company': '甲公司', 'url': 'https://a.example/careers'},
                               {'company': '乙公司'}],
                'candidate_count': 2,
                'company_waivers': [{'company': '甲公司', 'reason': '懒得看'}]}, ws)
        self.assertIn('官网链接', str(ctx.exception))

    def test_url_companies_become_verify_tasks_with_url(self):
        ws = workspace()
        config = make_config(channels=['official_career'])
        state = workflow.start_run(config, make_plan(config), ws)
        research = next(t for t in state['tasks'] if t['route'] == 'research')
        workflow.record_action(state, {
            'action': 'search', 'task_id': research['task_id'], 'status': 'success',
            'executed_at': '2026-10-31T16:01:00+08:00', 'exhausted': True,
            'identified': [{'company': '甲公司', 'url': 'https://a.example/careers'}],
            'candidate_count': 1,
            'derived': [{'company': '甲公司', 'url': 'https://a.example/careers'}]}, ws)
        kid = state['tasks'][-1]
        self.assertEqual(kid['route'], 'official_verify')
        self.assertEqual(kid['company_url'], 'https://a.example/careers')

    def test_official_budget_exhausted_marks_remaining_verify(self):
        ws = workspace()
        config = make_config(channels=['official_career'], budget=2)
        state = workflow.start_run(config, make_plan(config), ws)
        direct = next(t for t in state['tasks'] if t['route'] == 'direct')
        research = next(t for t in state['tasks'] if t['route'] == 'research')
        search(ws, state, direct['task_id'], count=0)
        search(ws, state, research['task_id'], count=3,
               identified=['甲', '乙', '丙'],
               derived=[{'company': '甲'}, {'company': '乙'}, {'company': '丙'}])
        # 预算 2 已被 direct+research 用光：剩余核实任务不能在关闭时静默消失
        workflow.close_stage(state, 'official')
        kids = [t for t in state['tasks'] if t['route'] == 'official_verify']
        self.assertTrue(all(t['status'] == 'budget_exhausted' for t in kids))
        self.assertEqual(state['stages']['official']['status'], 'closed_with_gaps')


class DeliveryGateTest(unittest.TestCase):
    def test_delivery_blocked_while_stage_open(self):
        ws = workspace()
        config = make_config(channels=['51job', 'bendibao_web'])
        state = workflow.start_run(config, make_plan(config), ws, user_confirmed_scope=True)
        task = tasks_of(state, channel='51job')[0]
        open_auth(ws, state, '51job')
        auth_probe(ws, state, '51job', LOGGED_IN_51JOB)
        search(ws, state, task['task_id'], exhausted=False)
        comp = workflow.finalize(state)
        self.assertFalse(comp['delivery_allowed'])
        self.assertTrue(comp['delivery_blockers'])

    def test_delivery_allowed_when_all_channels_closed(self):
        ws = workspace()
        config = make_config(channels=['51job'])
        state = workflow.start_run(config, make_plan(config), ws, user_confirmed_scope=True)
        comp = run_all_tasks(ws, state)
        self.assertTrue(comp['delivery_allowed'])
        self.assertEqual(comp['delivery_blockers'], [])
