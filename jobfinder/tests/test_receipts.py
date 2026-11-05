"""验收 8/9/10：回执字段不丢、支持同一轮分批提交、重放幂等且证据修订合法。"""
import unittest

import helpers
from jobfinder import planning, receipts, workflow


def _research_plan(config):
    tasks = [helpers.make_task("o1", channel="official_career", route="research")]
    return helpers.make_plan(config, tasks, channels=["official_career"])


def _receipt(plan, task, **overrides):
    config_fp = plan["config_fingerprint"]
    row = {"task_id": task["task_id"], "query": task["query"],
           "target_city": task["target_city"], "role_keyword": task["role_keyword"],
           "status": "success", "executed_at": helpers.iso(), "search_tool": "domain_search",
           "candidate_count": 1, "exhausted": True,
           "identified": [{"company": "甲"}, {"company": "乙"}],
           "derived": [{"company": "甲"}, {"company": "乙"}],
           "company_waivers": []}
    row.update(overrides)
    return {"mode": "agent_assisted", "plan_id": plan["plan_id"],
            "config_fingerprint": config_fp, "receipt_version": "receipt-2",
            "search_mode": "not_exposed", "search_log": [row],
            "candidates": [{"query": task["query"], "url": "https://example.com/1",
                            "title": "岗位", "discovered_at": helpers.iso(),
                            "source_channel": "official_career",
                            "task_ids": [task["task_id"]], "target_cities": ["北京"]}]}


class ReceiptFieldPreservationTests(unittest.TestCase):
    def test_new_fields_survive_validate_receipt(self):
        config = helpers.make_config(sources=["official_career"])
        plan = _research_plan(config)
        merged = planning.validate_receipt(config, plan, _receipt(plan, plan["tasks"][0]),
                                           run_protocol="receipt-2")
        row = merged[0]
        self.assertTrue(row["exhausted"])
        self.assertEqual(len(row["identified"]), 2)
        self.assertEqual(len(row["derived"]), 2)
        self.assertEqual(row["company_waivers"], [])
        self.assertEqual(row["receipt_version"], "receipt-2")
        self.assertTrue(row["receipt_id"])
        self.assertTrue(row["attempt_id"])

    def test_new_fields_survive_merge_into_run_state(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(sources=["official_career"])
            plan = _research_plan(config)
            state = workflow.start_run(config, plan, ws.path, channels=["official_career"],
                                       user_confirmed_scope=True)
            workflow.merge_receipt(state, _receipt(plan, plan["tasks"][0]), config, plan)
            task = state["tasks"][0]
            self.assertEqual(task["status"], "executed")
            self.assertTrue(task["exhausted"])
            self.assertEqual([x["company"] for x in task["identified"]], ["甲", "乙"])
            self.assertEqual(len(task["derived_ids"]), 2)
            self.assertTrue(all(t["route"] == "official_verify"
                                for t in state["tasks"] if t["task_id"].startswith("o1-c")))
            self.assertEqual(workflow.derived_gaps(state), [])

    def test_company_waivers_survive(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(sources=["official_career"])
            plan = _research_plan(config)
            state = workflow.start_run(config, plan, ws.path, channels=["official_career"],
                                       user_confirmed_scope=True)
            payload = helpers.search_receipt(
                "o1", exhausted=True, count=1,
                identified=[{"company": "甲"}, {"company": "乙"}],
                derived=[{"company": "甲"}],
                company_waivers=[{"company": "乙", "reason": "无公开官网"}])
            workflow.record_action(state, payload, ws.path)
            task = state["tasks"][0]
            self.assertEqual(task["company_waivers"],
                             [{"company": "乙", "reason": "无公开官网"}])
            self.assertEqual(len(task["derived_ids"]), 1)
            self.assertEqual(workflow.derived_gaps(state), [])

    def test_missing_required_field_raises_instead_of_degrading(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 5,
                                                 "per_channel": {"zhaopin": 5}})
            plan = helpers.make_plan(config, [helpers.make_task("z-1")], channels=["zhaopin"])
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            payload = helpers.search_receipt("z-1")
            with self.assertRaises(receipts.ReceiptError) as ctx:
                workflow.record_action(state, payload, ws.path)
            self.assertIn("exhausted", str(ctx.exception))
            self.assertIsNone(state["tasks"][0].get("legacy_exhaustion"))


class BatchSubmissionTests(unittest.TestCase):
    def test_two_batches_in_one_plan_and_run(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 6,
                                                 "per_channel": {"zhaopin": 6}})
            tasks = [helpers.make_task("z-1"), helpers.make_task("z-2")]
            plan = helpers.make_plan(config, tasks, channels=["zhaopin"])
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            first = [{"task_id": "z-1", "attempt_id": "att-1", "receipt_id": "r1"}]
            planning.ensure_new_execution(plan, first, ws.path, state=state, run_id=state["run_id"])
            accepted, duplicates = workflow.claim_receipts(state, first)
            self.assertEqual((len(accepted), len(duplicates)), (1, 0))
            workflow.record_action(state, helpers.search_receipt("z-1", exhausted=True), ws.path)
            second = [{"task_id": "z-2", "attempt_id": "att-2", "receipt_id": "r2"}]
            planning.ensure_new_execution(plan, second, ws.path, state=state, run_id=state["run_id"])
            accepted, duplicates = workflow.claim_receipts(state, second)
            self.assertEqual((len(accepted), len(duplicates)), (1, 0))
            workflow.record_action(state, helpers.search_receipt("z-2", exhausted=True), ws.path)
            self.assertEqual(state["tasks"][0]["status"], "executed")
            self.assertEqual(state["tasks"][1]["status"], "executed")
            self.assertEqual(state["budget_used"]["searches"]["北京"], 2)

    def test_replayed_batch_is_rejected_as_duplicate_but_not_as_a_new_plan(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 6,
                                                 "per_channel": {"zhaopin": 6}})
            plan = helpers.make_plan(config, [helpers.make_task("z-1")], channels=["zhaopin"])
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            batch = [{"task_id": "z-1", "attempt_id": "att-1", "receipt_id": "r1"}]
            workflow.claim_receipts(state, batch)
            with self.assertRaises(ValueError) as ctx:
                planning.ensure_new_execution(plan, batch, ws.path, state=state,
                                              run_id=state["run_id"])
            self.assertIn("已在本轮记录过", str(ctx.exception))

    def test_second_batch_not_blocked_by_first_export(self):
        """没有运行状态的历史入口：同一计划换一批任务仍然放行。"""
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 6,
                                                 "per_channel": {"zhaopin": 6}})
            plan = helpers.make_plan(config, [helpers.make_task("z-1")], channels=["zhaopin"])
            out = ws.path / "outputs" / "real" / "first"
            out.mkdir(parents=True)
            (out / "results.json").write_text(
                '{"discovery": {"plan_id": "%s", "operation": "new_search",'
                ' "search_log": [{"task_id": "z-1"}]}}' % plan["plan_id"], encoding="utf-8")
            batch2 = [{"task_id": "z-2", "attempt_id": "att-2", "executed_at": helpers.iso()}]
            planning.ensure_new_execution(plan, batch2, ws.path / "outputs" / "real")
            with self.assertRaises(ValueError):
                planning.ensure_new_execution(
                    plan, [{"task_id": "z-1", "attempt_id": "att-1",
                            "executed_at": helpers.iso()}], ws.path / "outputs" / "real")


class IdempotencyAndRevisionTests(unittest.TestCase):
    def _state(self, ws, channel_cap=1):
        config = helpers.make_config(budget={"searches_per_city": 5,
                                             "per_channel": {"zhaopin": channel_cap}})
        plan = helpers.make_plan(config, [helpers.make_task("z-1")], channels=["zhaopin"])
        return workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                  public_only=True, user_confirmed_scope=True)

    def test_replay_after_budget_exhaustion_still_succeeds(self):
        with helpers.Workspace() as ws:
            state = self._state(ws, channel_cap=1)
            payload = helpers.search_receipt("z-1", exhausted=False)
            first = workflow.record_action(state, payload, ws.path)
            self.assertNotIn("duplicate", first)
            # 配额只有 1 次，已用完；重放必须仍然成功（幂等优先于配额检查）
            replay = workflow.record_action(state, payload, ws.path)
            self.assertTrue(replay.get("duplicate"))
            self.assertEqual(state["budget_used"]["searches"]["北京"], 1)
            with self.assertRaises(ValueError):
                workflow.record_action(state, helpers.search_receipt("z-1", exhausted=True,
                                                                     attempt_id="att-new"),
                                       ws.path)

    def test_replay_after_stage_closed_is_still_idempotent(self):
        """阶段已推进后重放同一条已接受回执，不应被判成"跨阶段抢跑"。"""
        with helpers.Workspace() as ws:
            state = self._state(ws, channel_cap=5)
            payload = helpers.search_receipt("z-1", exhausted=True)
            workflow.record_action(state, payload, ws.path)
            self.assertEqual(state["stages"]["recruitment"]["status"], "closed")
            replay = workflow.record_action(state, payload, ws.path)
            self.assertTrue(replay.get("duplicate"))
            self.assertEqual(state["budget_used"]["searches"]["北京"], 1)

    def test_revision_adds_exhaustion_without_new_charge(self):
        with helpers.Workspace() as ws:
            state = self._state(ws, channel_cap=1)
            base = helpers.search_receipt("z-1", exhausted=False, attempt_id="att-1")
            workflow.record_action(state, base, ws.path)
            task = state["tasks"][0]
            self.assertEqual(task["attempts"], 1)
            self.assertFalse(task["exhausted"])
            revision = helpers.search_receipt("z-1", exhausted=True, attempt_id="att-1",
                                              evidence_ref="evidence/end-of-list.txt")
            result = workflow.record_action(state, revision, ws.path)
            self.assertTrue(result.get("revision"))
            self.assertEqual(task["attempts"], 1)
            self.assertTrue(task["exhausted"])
            self.assertEqual(task["evidence_ref"], "evidence/end-of-list.txt")
            self.assertEqual(state["budget_used"]["searches"]["北京"], 1)
            self.assertTrue(task["revisions"])
            self.assertIn("original_executed_at", task)

    def test_revision_allowed_when_budget_exhausted(self):
        with helpers.Workspace() as ws:
            state = self._state(ws, channel_cap=1)
            workflow.record_action(state, helpers.search_receipt("z-1", exhausted=False,
                                                                 attempt_id="att-1"), ws.path)
            result = workflow.record_action(
                state, helpers.search_receipt("z-1", exhausted=True, attempt_id="att-1"), ws.path)
            self.assertTrue(result.get("revision"))
            self.assertEqual(state["tasks"][0]["status"], "executed")


class LegacyReceiptTests(unittest.TestCase):
    def test_legacy_run_accepts_missing_exhausted_but_marks_unverified(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 5,
                                                 "per_channel": {"zhaopin": 5}})
            plan = helpers.make_plan(config, [helpers.make_task("z-1")], channels=["zhaopin"],
                                     receipt_protocol="receipt-1")
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            payload = helpers.search_receipt("z-1")
            payload["receipt_version"] = "receipt-1"
            workflow.record_action(state, payload, ws.path)
            task = state["tasks"][0]
            self.assertTrue(task["legacy_exhaustion"])
            self.assertEqual(task["exhausted"], None)
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertNotEqual(completion["verdict"], "COMPLETE")
            self.assertIn("未声明是否搜到底", "；".join(completion["reasons"]))


if __name__ == "__main__":
    unittest.main()
