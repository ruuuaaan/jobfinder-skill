"""历史兼容：旧运行记录、旧计划、旧回执、配置指纹都不被静默改写。"""
import json
import unittest

import helpers
from jobfinder import channels, planning, receipts, workflow
from jobfinder.spec import matching_config


OLD_STATE = {
    "schema_version": "workflow-run-1",
    "protocol_version": "workflow-1.1",
    "run_id": "run-old",
    "created_at": "2026-10-01T09:00:00+08:00",
    "updated_at": "2026-10-01T10:00:00+08:00",
    "plan_id": "plan-old",
    "config_fingerprint": "deadbeef",
    "scope": {"cities": ["北京"], "roles": ["财务BP"], "channels": ["zhaopin"],
              "stage_order": ["recruitment"], "platform_auth_policy": "public_only",
              "user_scope_explicit": True, "user_scope_confirmed": True},
    "current_stage": "recruitment",
    "stages": {"recruitment": {"status": "pending", "closed_at": None}},
    "tasks": [{"task_id": "北-1", "stage": "recruitment", "channel": "zhaopin",
               "route": "channel_search", "status": "executed", "attempts": 1,
               "executed_at": "2026-10-01T09:30:00+08:00", "candidate_count": 3,
               "query": "智联 北京 财务BP", "target_city": "北京",
               "role_keyword": "财务BP", "evidence_ref": "old.json"}],
    "auth": {},
    "budget_used": {"searches": {"北京": 1}, "by_stage": {"recruitment": {"北京": 1}},
                    "by_channel": {"zhaopin": {"北京": 1}}},
    "events": [{"kind": "search", "at": "2026-10-01T09:30:00+08:00"}],
    "derived_tasks": [],
    "scope_adjustments": [],
}


class LegacyStateTests(unittest.TestCase):
    def _write(self, ws):
        path = ws.path / "data" / "runs" / "run-old" / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(OLD_STATE, ensure_ascii=False), encoding="utf-8")
        return path

    def test_upgrade_keeps_history_and_marks_receipt_protocol(self):
        with helpers.Workspace() as ws:
            self._write(ws)
            state = workflow.load_state(ws.path, "run-old")
            self.assertEqual(state["upgraded_from"], "workflow-run-1")
            self.assertEqual(state["receipt_protocol"], receipts.V1)
            self.assertEqual(state["created_at"], "2026-10-01T09:00:00+08:00")
            task = state["tasks"][0]
            self.assertEqual(task["executed_at"], "2026-10-01T09:30:00+08:00")
            self.assertEqual(task["status"], "executed")
            self.assertIsNone(task["exhausted"])
            self.assertIn("query_scope", task)
            self.assertIn("pagination_chain_id", task)
            self.assertEqual(state["budget_used"]["searches"]["北京"], 1)

    def test_upgraded_state_finalizes_without_claiming_complete(self):
        with helpers.Workspace() as ws:
            self._write(ws)
            state = workflow.load_state(ws.path, "run-old")
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertNotEqual(completion["verdict"], "COMPLETE")
            self.assertIn("未声明是否搜到底", "；".join(completion["reasons"]))
            self.assertEqual(completion["receipt_protocol"], receipts.V1)

    def test_upgrade_is_idempotent(self):
        with helpers.Workspace() as ws:
            self._write(ws)
            first = workflow.load_state(ws.path, "run-old")
            workflow.save_state(first, ws.path)
            second = workflow.load_state(ws.path, "run-old")
            self.assertEqual(second.get("upgraded_from"), "workflow-run-1")
            self.assertEqual(second["tasks"][0]["executed_at"],
                             "2026-10-01T09:30:00+08:00")


class LegacyPlanTests(unittest.TestCase):
    def test_plan_without_budget_profile_or_scope_fields(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"per_city": {"北京": 3}})
            plan = {"plan_id": "plan-legacy", "created_at": helpers.iso(),
                    "config_fingerprint": planning.fingerprint(config),
                    "channels": ["zhaopin"],
                    "effective_config": config.model_dump(mode="json"),
                    "tasks": [{"task_id": "old-1", "target_city": "北京",
                               "role_keyword": "财务BP", "query": "智联 北京 财务BP",
                               "route": "channel_search", "channel": "zhaopin",
                               "stage": "recruitment", "status": "planned"}]}
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            self.assertEqual(state["scope"]["budget"]["per_city"], {"北京": 3})
            self.assertEqual(state["receipt_protocol"], receipts.V2)
            self.assertEqual(state["tasks"][0]["query_scope"], "财务BP")
            self.assertEqual(workflow.channel_budget_remaining(state, "zhaopin", "北京"), 3)


class FingerprintStabilityTests(unittest.TestCase):
    def test_offline_template_fingerprint_is_unchanged(self):
        """新增模块不得改变任何历史配置的指纹。"""
        path = helpers.SKILL / "assets" / "workspace" / "configs" / "offline.synthetic.json"
        config = matching_config(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(planning.fingerprint(config),
                         "93e4e8984a7f8782ff501ecddf9889f558e764214bbd69abb5815bca47d80581")

    def test_new_budget_fields_live_outside_the_config_model(self):
        config = helpers.make_config()
        self.assertNotIn("detail_reads_per_city", type(config.budget).model_fields)
        self.assertNotIn("list_pages_per_city", type(config.budget).model_fields)


class LegacyReceiptProtocolTests(unittest.TestCase):
    def _plan(self, config):
        return helpers.make_plan(config, [helpers.make_task("z-1")], channels=["zhaopin"],
                                 receipt_protocol="receipt-1")

    def test_legacy_receipt_accepted_only_under_legacy_protocol(self):
        config = helpers.make_config(sources=["zhaopin"])
        plan = self._plan(config)
        task = plan["tasks"][0]
        receipt = {"mode": "agent_assisted", "plan_id": plan["plan_id"],
                   "config_fingerprint": plan["config_fingerprint"],
                   "search_log": [{"task_id": "z-1", "query": task["query"],
                                   "target_city": "北京", "role_keyword": "财务BP",
                                   "status": "success", "executed_at": helpers.iso(),
                                   "search_tool": "old-tool", "candidate_count": 0}],
                   "candidates": []}
        merged = planning.validate_receipt(config, plan, receipt, run_protocol="receipt-1")
        self.assertEqual(merged[0]["receipt_version"], "receipt-1")
        with self.assertRaises(ValueError):
            planning.validate_receipt(config, plan, receipt, run_protocol="receipt-2")

    def test_unknown_receipt_version_is_rejected(self):
        with self.assertRaises(receipts.ReceiptError):
            receipts.resolve_version({"receipt_version": "receipt-9"})


class ChannelCoverageTests(unittest.TestCase):
    def test_enabled_but_untasked_channel_downgrades_completion(self):
        config = helpers.make_config(sources=["zhaopin", "campus_job"])
        completion = {"verdict": "COMPLETE", "reasons": [], "channels": [],
                      "partial_export": False, "next_action": None}
        result = workflow.completion_with_channels(completion, config, [])
        self.assertEqual(result["verdict"], "PARTIAL")
        self.assertIn("campus_job", [row["channel"] for row in result["channels"]])
        self.assertTrue(result["partial_export"])

    def test_all_supported_channels_have_stage_and_label(self):
        for name in channels.SUPPORTED:
            self.assertIn(name, channels.STAGE_ORDER and workflow.CHANNEL_STAGE)
            self.assertTrue(channels.label(name))
            self.assertTrue(channels.stage_of(name))


if __name__ == "__main__":
    unittest.main()
