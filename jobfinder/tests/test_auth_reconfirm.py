"""登录核验的"用户声明已登录"回补流程。

用户说"我登录了"但结构证据不足时：
- 不得据此放行搜索（不放宽 verified 门槛）；
- 也不得把用户的话当成没登录，而要给出"点进账号页面再核验"的具体指引；
- 同一渠道连续重试到上限后，回落 open_auth_tab 并提示按受阻处理。
"""
import unittest

import helpers
from jobfinder import platform_auth, workflow


def _state(ws, *, channel="51job"):
    config = helpers.make_config(sources=[channel])
    plan = helpers.make_plan(config, [helpers.make_task("g-1", channel=channel)],
                             channels=[channel])
    state = workflow.start_run(config, plan, ws.path, channels=[channel],
                               user_confirmed_scope=True, browser_surface="external")
    state["browser"] = {"backend": "external_browser", "status": "available",
                        "session_id": "sess-1", "installed": ["/fake/chrome"],
                        "capabilities": {}, "reason": None,
                        "requires_local_connector": False, "detected_by": "test"}
    return state


def _open(ws, state, *, channel="51job"):
    workflow.record_action(state, {"action": "auth_open", "channel": channel,
                                   "browser_session_id": "sess-1", "tab_id": "tab-1",
                                   "surface": "external_browser",
                                   "login_url": workflow.login_url(channel)}, ws.path)


def _snapshot(ws, state, *, header_text="", anchors=None):
    path = workflow.state_file(ws.path, state["run_id"]).parent / "auth" / "snap.json"
    return helpers.write_snapshot(
        path, text="我的51job 投递记录 账号设置",
        url="https://we.51job.com/pc/my/myjob", header_text=header_text,
        password_inputs=0,
        anchors=anchors if anchors is not None else [
            {"href": "https://we.51job.com/pc/my/myjob", "text": "我的51job", "in_header": False},
            {"href": "https://i.51job.com/userset/my_apply.php", "text": "投递记录", "in_header": False},
        ], target_id="tab-1", session_id="sess-1")


def _probe(ws, state, snap, *, channel="51job"):
    return workflow.record_action(state, {
        "action": "auth_probe", "channel": channel,
        "browser_session_id": "sess-1", "browser_context_id": "sess-1", "tab_id": "tab-1",
        "snapshot_path": str(snap), "url": "https://we.51job.com/pc/my/myjob",
        "session_scope": "browser_visible_dom"}, ws.path)


def _claim(state, *, channel="51job"):
    return workflow.record_action(state, {"action": "user_claimed_login", "channel": channel,
                                          "reason": "我登录了"}, None)


class HeaderSignalTests(unittest.TestCase):
    def test_header_not_captured_is_reported_as_signal_gap(self):
        """头部抓不到时，文案要说明这是抓不到判定信号，而不是"未登录"。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            result = _probe(ws, state, _snapshot(ws, state))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["header_signal"], "header_not_captured")
            joined = " ".join(result["problems"])
            self.assertIn("不代表未登录", joined)

    def test_missing_session_is_not_masked_by_reconfirm(self):
        """没有真实打开事件时，声明登录也不能绕过绑定检查。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            with self.assertRaises(ValueError):
                workflow.record_action(state, {"action": "user_claimed_login",
                                               "channel": "51job"}, ws.path)


class ClaimReconfirmTests(unittest.TestCase):
    def test_claim_moves_state_to_reconfirm_required(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            _probe(ws, state, _snapshot(ws, state))
            result = _claim(state)
            self.assertEqual(result["status"], "reconfirm_required")
            self.assertEqual(result["claim_count"], 1)
            steps = result["reconfirm"]["steps"]
            self.assertTrue(any("我的51job" in s for s in steps))

    def test_next_action_asks_user_to_reconfirm_instead_of_reopening_login(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            _probe(ws, state, _snapshot(ws, state))
            _claim(state)
            action = workflow.next_action(state)
            self.assertEqual(action["action"], "wait_user_reconfirm")
            self.assertEqual(action["channel"], "51job")
            self.assertTrue(action["reconfirm"]["steps"])
            self.assertLess(action["reconfirm"]["attempts"], action["reconfirm"]["limit"])

    def test_claim_does_not_allow_business_search(self):
        """声明登录不等于核验通过：业务回执仍必须被拒。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            _probe(ws, state, _snapshot(ws, state))
            _claim(state)
            with self.assertRaises(ValueError):
                workflow.record_action(state, {
                    "action": "search", "task_id": "g-1", "status": "success",
                    "executed_at": "2026-01-01T10:00:00+08:00",
                    "search_tool": "test", "candidate_count": 0, "exhausted": True,
                    "identified": []}, ws.path)

    def test_gives_up_after_limit_and_points_to_blocked(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            _probe(ws, state, _snapshot(ws, state))
            _claim(state)
            state["auth"]["51job"]["reconfirm_attempts"] = platform_auth.RECONFIRM_LIMIT
            action = workflow.next_action(state)
            self.assertEqual(action["action"], "open_auth_tab")
            self.assertIn("blocked", action["message"])

    def test_repeated_failed_probes_increment_attempts_and_keep_claim(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            snap = _snapshot(ws, state)
            _probe(ws, state, snap)
            _claim(state)
            second = _probe(ws, state, snap)
            self.assertEqual(second["status"], "failed")
            self.assertEqual(state["auth"]["51job"]["reconfirm_attempts"], 2)
            self.assertTrue(state["auth"]["51job"].get("user_claim"))


class VerifiedPathStillWorks(unittest.TestCase):
    def test_header_marker_restores_verification(self):
        """补上头部信号后应能通过，证明重试路径不是死路。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            snap = _snapshot(ws, state, header_text="首页 搜索 在线简历 我的51Job 账号设置",
                             anchors=[{"href": "https://we.51job.com/pc/my/myjob",
                                       "text": "我的51job", "in_header": True},
                                      {"href": "https://i.51job.com/userset/my_apply.php",
                                       "text": "投递记录", "in_header": True}])
            result = _probe(ws, state, snap)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(workflow.auth_status(state, "51job"), "verified")


if __name__ == "__main__":
    unittest.main()
