"""计划生成缺陷回归：官网任务、列表页回退、未适配城市。"""
import unittest

from support import make_config, make_plan

from jobfinder import workflow
from jobfinder.planning import BROWSE_CATALOG, browse_covered_pairs, browse_rows
from jobfinder.workflow import channel_stage


class PlanChannelTest(unittest.TestCase):
    def test_official_tasks_only_when_official_enabled(self):
        plan = make_plan(make_config(channels=['51job']))
        self.assertEqual([t['channel'] for t in plan['tasks']], ['51job'])
        self.assertFalse(any(t['route'] in ('direct', 'research', 'enterprise') for t in plan['tasks']))
        self.assertEqual(plan['stage_order'], ['recruitment'])

    def test_official_enabled_generates_direct_and_research(self):
        plan = make_plan(make_config(channels=['official_career', '51job']))
        routes = {t['route'] for t in plan['tasks'] if t['channel'] == 'official_career'}
        self.assertIn('direct', routes)
        self.assertIn('research', routes)

    def test_beijing_51job_falls_back_to_onsite_search(self):
        plan = make_plan(make_config(channels=['51job'], city='北京'))
        task = plan['tasks'][0]
        self.assertEqual(task['route'], 'channel_search')
        self.assertIn('北京', task['query'])
        self.assertEqual(task['stage'], 'recruitment')

    def test_browse_city_adaptation_is_required_for_fallback_removal(self):
        # 上海已适配、北京未适配：同一渠道不得因类目命中就丢掉北京的搜索回退。
        self.assertFalse(browse_rows('北京', ['薪酬经理'], ['51job']))
        self.assertTrue(browse_rows('上海', ['薪酬经理'], ['51job']))
        self.assertEqual(browse_covered_pairs('北京', ['薪酬经理'], ['51job']), set())
        self.assertIn(('51job', '薪酬经理'), browse_covered_pairs('上海', ['薪酬经理'], ['51job']))

    def test_every_task_carries_stage(self):
        plan = make_plan(make_config(channels=['official_career', '51job', 'bendibao_web']))
        for task in plan['tasks']:
            self.assertEqual(task['stage'], channel_stage(task['channel']))

    def test_catalog_entries_are_tested_combinations_only(self):
        for channel, spec in BROWSE_CATALOG.items():
            self.assertTrue(spec['categories'])
            for keywords, slug, label in spec['categories']:
                self.assertTrue(keywords and slug and label)


if __name__ == '__main__':
    unittest.main()


class ChannelMenuTest(unittest.TestCase):
    def test_menu_lists_every_supported_channel_by_stage(self):
        menu = workflow.channel_menu(make_config(channels=['51job']))
        flat = [c['channel'] for row in menu['menu'] for c in row['channels']]
        self.assertEqual(flat, list(workflow.SUPPORTED_CHANNELS))
        self.assertIn('boss', flat)
        self.assertIn('liepin', flat)
        self.assertEqual([row['stage'] for row in menu['menu']], list(workflow.STAGES))
        in_config = {c['channel']: c['in_config'] for row in menu['menu'] for c in row['channels']}
        self.assertTrue(in_config['51job'])
        self.assertFalse(in_config['boss'])

    def test_selection_must_be_explicit(self):
        with self.assertRaises(ValueError):
            workflow.resolve_channel_selection([], False)
        with self.assertRaises(ValueError):
            workflow.resolve_channel_selection(['nope'], False)
        picked, all_flag = workflow.resolve_channel_selection(['51job', 'boss'], False)
        self.assertEqual(picked, ['51job', 'boss'])
        self.assertFalse(all_flag)
        picked, all_flag = workflow.resolve_channel_selection(None, True)
        self.assertEqual(tuple(picked), workflow.SUPPORTED_CHANNELS)
        self.assertTrue(all_flag)
