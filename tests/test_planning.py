"""计划生成缺陷回归：官网任务、列表页回退、未适配城市。"""
import unittest

from support import make_config, make_plan

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
