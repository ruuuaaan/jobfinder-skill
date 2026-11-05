"""验收 5/7：完成度按独立搜索范围计算；预算耗尽不能伪装成"网站没有更多结果"。"""
import unittest

import helpers
from jobfinder import workflow


class ScopeGranularityTests(unittest.TestCase):
    def _state(self, ws, tasks, *, cities=("北京", "深圳"), budget=None):
        config = helpers.make_config(target_cities=list(cities),
                                     budget=budget or {"searches_per_city": 10,
                                                       "per_channel": {"zhaopin": 6}})
        plan = helpers.make_plan(config, tasks, channels=["zhaopin"])
        return config, workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                          public_only=True, user_confirmed_scope=True)

    def test_other_city_still_open_blocks_complete(self):
        """北京到底、深圳没到底：不能判整轮 COMPLETE，也不能关闭阶段。"""
        with helpers.Workspace() as ws:
            tasks = [helpers.make_task("bj-1", city="北京"),
                     helpers.make_task("sz-1", city="深圳")]
            config, state = self._state(ws, tasks)
            workflow.record_action(state, helpers.search_receipt("bj-1", exhausted=True),
                                   ws.path)
            workflow.record_action(state, helpers.search_receipt("sz-1", exhausted=False, count=3),
                                   ws.path)
            scopes = {s["city"]: s for s in workflow.scope_completion(state)}
            self.assertTrue(scopes["北京"]["exhausted"])
            self.assertFalse(scopes["深圳"]["exhausted"])
            with self.assertRaises(ValueError) as ctx:
                workflow.close_stage(state, "recruitment", workspace=ws.path)
            self.assertIn("深圳", str(ctx.exception))
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertNotEqual(completion["verdict"], "COMPLETE")
            self.assertFalse(completion["delivery_allowed"])

    def test_other_role_still_open_blocks_channel(self):
        """同城同平台：岗位词 A 到底，不代表岗位词 B 到底。"""
        with helpers.Workspace() as ws:
            tasks = [helpers.make_task("a-1", role="财务BP"),
                     helpers.make_task("b-1", role="人力资源")]
            config, state = self._state(ws, tasks, cities=("北京",))
            workflow.record_action(state, helpers.search_receipt("a-1", exhausted=True), ws.path)
            workflow.record_action(state, helpers.search_receipt("b-1", exhausted=False), ws.path)
            action = workflow.next_action(state)
            self.assertEqual(action["action"], "continue_channel")
            self.assertEqual(action["task_id"], "b-1")
            completion = workflow.finalize(state, workspace=ws.path)
            channel = next(c for c in completion["channels"] if c["channel"] == "zhaopin")
            self.assertEqual(channel["not_exhausted"], 1)
            self.assertEqual(channel["scopes"], 2)
            self.assertNotEqual(completion["verdict"], "COMPLETE")

    def test_last_page_closes_only_its_chain(self):
        """分页链末页到底，只关闭这条链；同平台其他链仍然开放。"""
        with helpers.Workspace() as ws:
            tasks = [
                helpers.make_task("p1", route="browse_list", browse_url="https://jobs.example/a",
                                  browse_page=1),
                helpers.make_task("p2", route="browse_list", browse_url="https://jobs.example/a",
                                  browse_page=2),
                helpers.make_task("q1", route="browse_list", browse_url="https://jobs.example/b",
                                  browse_page=1),
            ]
            config, state = self._state(ws, tasks, cities=("北京",))
            workflow.record_action(state, helpers.search_receipt("p1", exhausted=False), ws.path)
            workflow.record_action(state, helpers.search_receipt("p2", exhausted=True), ws.path)
            keys = {s["pagination_chain_id"] for s in workflow.scope_completion(state)}
            self.assertEqual(keys, {"https://jobs.example/a", "https://jobs.example/b"})
            chain_a = next(s for s in workflow.scope_completion(state)
                           if s["pagination_chain_id"] == "https://jobs.example/a")
            chain_b = next(s for s in workflow.scope_completion(state)
                           if s["pagination_chain_id"] == "https://jobs.example/b")
            self.assertTrue(chain_a["exhausted"])
            self.assertFalse(chain_b["exhausted"])
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertNotEqual(completion["verdict"], "COMPLETE")

    def test_derived_page_inherits_chain(self):
        """register_derived(kind=page) 派生下一页时继承同一条链。"""
        with helpers.Workspace() as ws:
            tasks = [helpers.make_task("p1", route="browse_list",
                                       browse_url="https://jobs.example/a", browse_page=1)]
            config, state = self._state(ws, tasks, cities=("北京",))
            workflow.record_action(state, helpers.search_receipt("p1", exhausted=False), ws.path)
            workflow.record_action(state, {"action": "register_derived", "task_id": "p1",
                                           "kind": "page",
                                           "items": [{"url": "https://jobs.example/a/p2",
                                                      "page": 2}]}, ws.path)
            child = next(t for t in state["tasks"] if t["task_id"].startswith("p1-p"))
            self.assertEqual(child["pagination_chain_id"], "https://jobs.example/a")
            self.assertTrue(state["tasks"][0]["continued"])


class BudgetExhaustionTests(unittest.TestCase):
    def test_budget_ceiling_is_a_gap_not_an_exhausted_site(self):
        """配额用尽只能记覆盖缺口：状态是 budget_exhausted，原因不能写"无更多结果"。"""
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 2,
                                                 "per_channel": {"official_career": 2}})
            tasks = [helpers.make_task("o1", channel="official_career", route="direct"),
                     helpers.make_task("o2", channel="official_career", route="direct")]
            plan = helpers.make_plan(config, tasks, channels=["official_career"])
            state = workflow.start_run(config, plan, ws.path, channels=["official_career"],
                                       user_confirmed_scope=True)
            workflow.record_action(state, helpers.search_receipt("o1", exhausted=False), ws.path)
            workflow.record_action(state, helpers.search_receipt("o2", exhausted=False), ws.path)
            self.assertEqual(workflow.budget_remaining(state, "北京"), 0)
            closed = workflow.close_stage(state, "official", workspace=ws.path)
            self.assertEqual(closed["status"], "closed_with_gaps")
            for task in state["tasks"]:
                self.assertEqual(task["status"], "budget_exhausted")
                self.assertIn("配额已用尽", task["reason"])
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertEqual(completion["verdict"], "PARTIAL")
            reasons = "；".join(completion["reasons"])
            self.assertIn("预算", reasons)
            self.assertNotIn("无更多结果", reasons)

    def test_official_quota_exhausted_with_pending_verifications_can_close(self):
        """官网配额耗尽仍有派生核实任务：能正常记缺口并收尾，不会卡死在无法执行又无法收尾。"""
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 3,
                                                 "per_channel": {"official_career": 2}})
            tasks = [helpers.make_task("o1", channel="official_career", route="research")]
            plan = helpers.make_plan(config, tasks, channels=["official_career"])
            state = workflow.start_run(config, plan, ws.path, channels=["official_career"],
                                       user_confirmed_scope=True)
            workflow.record_action(state, helpers.search_receipt(
                "o1", exhausted=True, count=2,
                identified=[{"company": "甲"}, {"company": "乙"}],
                derived=[{"company": "甲"}, {"company": "乙"}]), ws.path)
            verify_tasks = [t for t in state["tasks"] if t["route"] == "official_verify"]
            self.assertEqual(len(verify_tasks), 2)
            workflow.record_action(state, helpers.search_receipt(
                verify_tasks[0]["task_id"], exhausted=None, status="success"), ws.path)
            closed = workflow.close_stage(state, "official", workspace=ws.path)
            self.assertEqual(closed["status"], "closed_with_gaps")
            pending = [t for t in state["tasks"] if t["status"] == "budget_exhausted"]
            self.assertEqual(len(pending), 1)
            self.assertIn("官网", pending[0]["reason"])
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertEqual(completion["verdict"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
