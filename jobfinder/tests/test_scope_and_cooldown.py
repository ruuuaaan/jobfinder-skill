"""验收 11/12：范围确认不能被助手冒认；平台冷却跨 run 保留且不阻塞其它渠道。"""
import unittest

import helpers
from jobfinder import access_control, workflow


class UserScopeConfirmationTests(unittest.TestCase):
    def _run(self, ws, *, user_confirmed):
        config = helpers.make_config(sources=["official_career", "zhaopin"])
        plan = helpers.make_plan(config,
                                 [helpers.make_task("z-1"), helpers.make_task("o-1",
                                                                              channel="official_career",
                                                                              route="direct")],
                                 channels=["official_career", "zhaopin"])
        return workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                  public_only=True, user_confirmed_scope=user_confirmed)

    def test_agent_narrowing_is_not_recorded_as_user_confirmation(self):
        """11. 未确认的渠道缩小不能记为用户已确认。"""
        with helpers.Workspace() as ws:
            state = self._run(ws, user_confirmed=False)
            scope = state["scope"]
            self.assertFalse(scope["user_scope_confirmed"])
            self.assertEqual(scope["narrowed_by"], "agent")
            self.assertIn("official", scope["missing_stages"])
            self.assertNotIn("recruitment", scope["missing_stages"])
            self.assertEqual(state["scope_adjustments"][0]["narrowed_by"], "agent")
            workflow.record_action(state, helpers.search_receipt("z-1", exhausted=True), ws.path)
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertEqual(completion["verdict"], "PARTIAL")
            self.assertFalse(completion["scope_confirmed"])
            self.assertIn("未经用户确认", "；".join(completion["reasons"]))

    def test_user_confirmed_narrowing_can_complete(self):
        with helpers.Workspace() as ws:
            state = self._run(ws, user_confirmed=True)
            self.assertTrue(state["scope"]["user_scope_confirmed"])
            self.assertEqual(state["scope"]["narrowed_by"], "user")
            workflow.record_action(state, helpers.search_receipt("z-1", exhausted=True), ws.path)
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertEqual(completion["verdict"], "COMPLETE")
            self.assertIn("用户明确收窄", "；".join(completion["reasons"]))

    def test_cli_flag_is_honoured(self):
        """--user-confirmed 必须真的影响状态，而不是固定传 True。"""
        import inspect
        from jobfinder import __main__ as cli
        source = inspect.getsource(cli.main)
        self.assertIn("user_confirmed_scope=bool(args.user_confirmed)", source)
        self.assertNotIn("user_confirmed_scope=True", source)


class PlatformCooldownTests(unittest.TestCase):
    def _config(self, ws):
        return helpers.make_config(sources=["zhaopin", "51job"],
                                   budget={"searches_per_city": 10,
                                           "per_channel": {"zhaopin": 5, "51job": 5}})

    def _run(self, ws):
        config = self._config(ws)
        plan = helpers.make_plan(config,
                                 [helpers.make_task("z-1", channel="zhaopin"),
                                  helpers.make_task("j-1", channel="51job")],
                                 channels=["zhaopin", "51job"])
        return config, plan, workflow.start_run(config, plan, ws.path,
                                                channels=["zhaopin", "51job"],
                                                public_only=True, user_confirmed_scope=True)

    def test_cooldown_survives_new_run_and_other_channel_proceeds(self):
        """12. 风控冷却跨 run 保留；其他可访问渠道仍按规则推进。"""
        with helpers.Workspace() as ws:
            access_control.note_restricted(ws.path, "zhaopin", kind="rate_limited",
                                           reason="HTTP 429 Too Many Requests",
                                           retry_after=3600, run_id="run-old")
            view = access_control.cooldown(ws.path, "zhaopin")
            self.assertEqual(view["state"], "cooling")
            config, plan, state = self._run(ws)
            self.assertIn("zhaopin", state["platform_state"])
            action = workflow.next_action(state)
            self.assertEqual(action["action"], "search_task")
            self.assertEqual(action["task_id"], "j-1")
            # 换关键词/城市/新 run 都不清除
            workflow.record_action(state, helpers.search_receipt("j-1", exhausted=True,
                                                                 channel="51job"), ws.path)
            action = workflow.next_action(state)
            self.assertIn(action["action"], ("channel_cooling", "channel_blocked"))
            self.assertEqual(action["channel"], "zhaopin")
            config2, plan2, state2 = self._run(ws)
            self.assertIn("zhaopin", state2["platform_state"])

    def test_blocked_channel_recorded_then_stage_can_close(self):
        with helpers.Workspace() as ws:
            access_control.note_restricted(ws.path, "zhaopin", kind="rate_limited",
                                           reason="429", retry_after=3600)
            config, plan, state = self._run(ws)
            workflow.record_action(state, helpers.search_receipt("j-1", exhausted=True,
                                                                 channel="51job"), ws.path)
            workflow.record_action(state, {"action": "blocked", "channel": "zhaopin",
                                           "reason": "平台 429 冷却中（跨 run 保留）",
                                           "evidence_ref": "data/state/platforms.json"}, ws.path)
            closed = workflow.close_stage(state, "recruitment", workspace=ws.path)
            self.assertEqual(closed["status"], "closed_with_gaps")
            completion = workflow.finalize(state, workspace=ws.path)
            self.assertEqual(completion["verdict"], "PARTIAL")

    def test_cooldown_cannot_be_cleared_by_assistant(self):
        with helpers.Workspace() as ws:
            access_control.note_restricted(ws.path, "zhaopin", kind="rate_limited",
                                           reason="429", retry_after=3600)
            with self.assertRaises(PermissionError):
                access_control.clear_cooldown(ws.path, "zhaopin")
            record = access_control.clear_cooldown(ws.path, "zhaopin", user_confirmed=True)
            self.assertNotIn("cooldown_until", record)
            self.assertIsNone(access_control.cooldown(ws.path, "zhaopin"))

    def test_restricted_receipt_persists_to_disk(self):
        with helpers.Workspace() as ws:
            config, plan, state = self._run(ws)
            workflow.record_action(state, helpers.search_receipt(
                "z-1", status="restricted", channel="zhaopin", exhausted=None,
                reason="HTTP 429 频率限制"), ws.path)
            stored = access_control.channel_state(ws.path, "zhaopin")
            self.assertEqual(stored.get("last_status"), "rate_limited")
            self.assertTrue(stored.get("cooldown_until"))
            self.assertEqual(workflow.auth_status(state, "zhaopin"), "public_only")


if __name__ == "__main__":
    unittest.main()
