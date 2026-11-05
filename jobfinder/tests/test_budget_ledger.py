"""验收 6：per_city 与 per_channel 在计划、执行、恢复、收尾中使用同一套口径。"""
import unittest

import helpers
from jobfinder import budgets, workflow


class BudgetLedgerTests(unittest.TestCase):
    def _config(self):
        return helpers.make_config(
            target_cities=["北京", "深圳"],
            budget={"searches_per_city": 40,
                    "per_city": {"北京": 7, "深圳": 4},
                    "per_channel": {"official_career": 3, "zhaopin": 2}})

    def test_freeze_keeps_per_city_and_channel_profile(self):
        """start_run 必须冻结 budget.per_city，不能只剩 searches_per_city。"""
        with helpers.Workspace() as ws:
            config = self._config()
            plan = helpers.make_plan(config, [helpers.make_task("z-1")],
                                     channels=["zhaopin"],
                                     budget_profile={"per_city_channel": {"北京": {"zhaopin": 5}},
                                                     "list_pages_per_city": 2})
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            frozen = state["scope"]["budget"]
            self.assertEqual(frozen["per_city"], {"北京": 7, "深圳": 4})
            self.assertEqual(frozen["per_channel"], {"official_career": 3, "zhaopin": 2})
            self.assertEqual(frozen["per_city_channel"]["北京"]["zhaopin"], 5)
            # 渠道×城市 > 渠道 > 城市 > 全局
            self.assertEqual(budgets.cap(frozen, "zhaopin", "北京"), 5)
            self.assertEqual(budgets.cap(frozen, "zhaopin", "深圳"), 2)
            self.assertEqual(budgets.cap(frozen, "official_career", "北京"), 3)
            self.assertEqual(budgets.cap(frozen, "campus_job", "北京"), 7)
            self.assertEqual(budgets.cap(frozen, "campus_job", "广州"), 40)

    def test_dispatch_and_close_use_same_limit(self):
        """派发说没额度，收尾也必须按同一额度判缺口（旧版两套账）。"""
        with helpers.Workspace() as ws:
            config = self._config()
            tasks = [helpers.make_task(f"z-{i}") for i in range(1, 8)]
            plan = helpers.make_plan(config, tasks, channels=["zhaopin"],
                                     budget_profile={"per_city_channel": {"北京": {"zhaopin": 5}}})
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            for index in range(1, 6):
                workflow.record_action(state, helpers.search_receipt(f"z-{index}", exhausted=False),
                                       ws.path)
            # 第 6 条：派发拒绝
            action = workflow.next_action(state)
            self.assertNotIn(action.get("task_id"), {"z-6", "z-7"})
            with self.assertRaises(ValueError):
                workflow.record_action(state, helpers.search_receipt("z-6", exhausted=False),
                                       ws.path)
            self.assertEqual(workflow.task_budget_remaining(state, state["tasks"][5]), 0)
            closed = workflow.close_stage(state, "recruitment", workspace=ws.path)
            self.assertEqual(closed["status"], "closed_with_gaps")
            self.assertEqual([t["status"] for t in state["tasks"][5:]],
                             ["budget_exhausted", "budget_exhausted"])

    def test_recovery_keeps_consumption_and_does_not_reset(self):
        """恢复运行不重置预算，也不重复扣同一笔。"""
        with helpers.Workspace() as ws:
            config = self._config()
            tasks = [helpers.make_task(f"z-{i}") for i in range(1, 5)]
            plan = helpers.make_plan(config, tasks, channels=["zhaopin"])
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            receipt = helpers.search_receipt("z-1", exhausted=False)
            workflow.record_action(state, receipt, ws.path)
            workflow.record_action(state, receipt, ws.path)   # 同一条回执重放
            reloaded = workflow.load_state(ws.path, state["run_id"])
            self.assertEqual(budgets.used(reloaded, "zhaopin", "北京"), 1)
            self.assertEqual(reloaded["budget_used"]["searches"]["北京"], 1)
            self.assertEqual(budgets.total_remaining(reloaded, "北京"), 1)

    def test_failed_attempt_counts_once_per_attempt(self):
        """失败也算一次消耗，但同一次失败重放不会再扣。"""
        with helpers.Workspace() as ws:
            config = self._config()
            plan = helpers.make_plan(config, [helpers.make_task("z-1")],
                                     channels=["zhaopin"],
                                     budget_profile={"per_city_channel": {"北京": {"zhaopin": 2}}})
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            failed = helpers.search_receipt("z-1", status="failed", exhausted=None,
                                            attempt_id="att-1", reason="timeout")
            workflow.record_action(state, failed, ws.path)
            workflow.record_action(state, failed, ws.path)
            self.assertEqual(budgets.used(state, "zhaopin", "北京"), 1)
            self.assertEqual(state["tasks"][0]["status"], "failed")

    def test_list_page_budget_is_separate_from_search_budget(self):
        """列表页访问单独计量：搜索次数没用完，也不能无限翻页。"""
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 20,
                                                 "per_channel": {"zhaopin": 10}})
            tasks = [helpers.make_task(f"p{i}", route="browse_list",
                                       browse_url=f"https://jobs.example/{i}", browse_page=1)
                     for i in (1, 2, 3)]
            plan = helpers.make_plan(config, tasks, channels=["zhaopin"],
                                     budget_profile={"list_pages_per_city": 2})
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            workflow.record_action(state, helpers.search_receipt("p1", exhausted=False), ws.path)
            workflow.record_action(state, helpers.search_receipt("p2", exhausted=False), ws.path)
            self.assertEqual(workflow.budget_remaining(state, "北京"), 8)  # 搜索预算还有
            self.assertEqual(budgets.kind_remaining(state, budgets.LIST_PAGE, "北京"), 0)
            with self.assertRaises(ValueError) as ctx:
                workflow.record_action(state, helpers.search_receipt("p3", exhausted=False),
                                       ws.path)
            self.assertIn("list_page", str(ctx.exception))
            self.assertEqual(budgets.used(state, "zhaopin", "北京", budgets.LIST_PAGE), 2)

    def test_stage_total_is_reporting_only(self):
        """阶段账只用于报告，不再作为第二道门。"""
        with helpers.Workspace() as ws:
            config = self._config()
            plan = helpers.make_plan(config, [helpers.make_task("z-1")], channels=["zhaopin"])
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            workflow.record_action(state, helpers.search_receipt("z-1", exhausted=True), ws.path)
            self.assertEqual(workflow.stage_budget_used(state, "recruitment", "北京"), 1)
            self.assertEqual(workflow.stage_budget_remaining(state, "recruitment", "北京"), 1)
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertIn("scopes", completion)


if __name__ == "__main__":
    unittest.main()
