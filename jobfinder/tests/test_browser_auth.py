"""验收 1/2/3/4：登录必须来自真实浏览器打开事件与账号区域证据；不静默回退内嵌预览。"""
import unittest
from unittest import mock

import helpers
from jobfinder import browser_backend, platform_auth, workflow


def _state(ws, *, channel="linkedin", surface="external"):
    config = helpers.make_config(sources=[channel])
    plan = helpers.make_plan(config, [helpers.make_task("l-1", channel=channel)],
                             channels=[channel])
    state = workflow.start_run(config, plan, ws.path, channels=[channel],
                               user_confirmed_scope=True, browser_surface=surface)
    state["browser"] = {"backend": "external_browser", "status": "available",
                        "session_id": "sess-1", "installed": ["/fake/chrome"],
                        "capabilities": {}, "reason": None,
                        "requires_local_connector": False, "detected_by": "test"}
    return state


def _open(state, ws, *, channel="linkedin", session="sess-1", tab="tab-1"):
    workflow.record_action(state, {"action": "auth_open", "channel": channel,
                                   "browser_session_id": session, "tab_id": tab,
                                   "surface": "external_browser",
                                   "login_url": workflow.login_url(channel)}, ws.path)


def _probe(ws, state, snapshot, *, channel="linkedin", session="sess-1", tab="tab-1"):
    return workflow.record_action(state, {
        "action": "auth_probe", "channel": channel,
        "browser_session_id": session, "browser_context_id": session, "tab_id": tab,
        "snapshot_path": str(snapshot), "url": snapshot.get("url") if isinstance(snapshot, dict) else None,
        "session_scope": "browser_visible_dom"}, ws.path)


def _logged_in_snapshot(ws, state, *, text="我的网络 My Network\n消息 Messaging\n通知",
                        url="https://www.linkedin.com/feed/", anchors=None,
                        header_text="My Network Messaging Notifications Me",
                        password_inputs=0, tab="tab-1", session="sess-1"):
    path = workflow.state_file(ws.path, state["run_id"]).parent / "auth" / "snap.json"
    return helpers.write_snapshot(
        path, text=text, url=url, header_text=header_text, password_inputs=password_inputs,
        anchors=anchors or [
            {"href": "/mynetwork/", "text": "My Network", "in_header": True},
            {"href": "/messaging/", "text": "Messaging", "in_header": True},
            {"href": "/me/", "text": "Me", "in_header": True},
        ], target_id=tab, session_id=session)


class OpenEventTests(unittest.TestCase):
    def test_snapshot_with_login_words_without_open_event_is_rejected(self):
        """1. 无浏览器打开事件时，仅提交含登录文字的页面快照不能通过。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            snapshot = _logged_in_snapshot(ws, state)
            result = workflow.record_action(state, {
                "action": "auth_probe", "channel": "linkedin",
                "browser_session_id": "sess-1", "tab_id": "tab-1",
                "snapshot_path": str(snapshot), "url": "https://www.linkedin.com/feed/",
                "session_scope": "browser_visible_dom"}, ws.path)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("打开事件" in p for p in result["problems"]))
            self.assertNotEqual(workflow.auth_status(state, "linkedin"), "verified")

    def test_open_action_without_browser_handle_is_rejected(self):
        """打印登录链接不能记成浏览器已打开。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            with self.assertRaises(ValueError) as ctx:
                workflow.record_action(state, {"action": "auth_open", "channel": "linkedin",
                                               "login_url": workflow.login_url("linkedin"),
                                               "opened_url_only": True}, ws.path)
            self.assertIn("登录链接", str(ctx.exception))

    def test_open_event_requires_session_and_tab(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            with self.assertRaises(platform_auth.ProbeError):
                platform_auth.build_open_record("linkedin", {"session_id": None,
                                                             "target_id": None})

    def test_verified_when_account_region_is_present(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(state, ws)
            snapshot = _logged_in_snapshot(ws, state)
            result = workflow.record_action(state, {
                "action": "auth_probe", "channel": "linkedin",
                "browser_session_id": "sess-1", "browser_context_id": "sess-1",
                "tab_id": "tab-1", "snapshot_path": str(snapshot),
                "url": "https://www.linkedin.com/feed/",
                "session_scope": "browser_visible_dom"}, ws.path)
            self.assertEqual(result["status"], "verified", result["problems"])
            self.assertEqual(workflow.auth_status(state, "linkedin"), "verified")
            record = state["auth"]["linkedin"]
            self.assertTrue(record["account_links_matched"])
            self.assertIn("account_region", record["verification_basis"])


class SecurityPageTests(unittest.TestCase):
    def test_english_checkpoint_with_my_network_is_not_verified(self):
        """2. 英文安全验证页面即使包含 My Network，也不能判 verified。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(state, ws)
            snapshot = _logged_in_snapshot(
                ws, state,
                url="https://www.linkedin.com/checkpoint/challenge/123",
                text="Security check\nWe've restricted your account\nMy Network\nPlease verify you are human",
                header_text="My Network")
            result = workflow.record_action(state, {
                "action": "auth_probe", "channel": "linkedin",
                "browser_session_id": "sess-1", "tab_id": "tab-1",
                "snapshot_path": str(snapshot),
                "url": "https://www.linkedin.com/checkpoint/challenge/123",
                "session_scope": "browser_visible_dom"}, ws.path)
            self.assertEqual(result["status"], "failed")
            joined = "；".join(result["problems"])
            self.assertIn("登录/风控页面", joined)
            self.assertIn("验证", joined)

    def test_login_form_with_password_input_is_not_verified(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(state, ws)
            snapshot = _logged_in_snapshot(ws, state, password_inputs=1,
                                           url="https://www.linkedin.com/login")
            result = workflow.record_action(state, {
                "action": "auth_probe", "channel": "linkedin",
                "browser_session_id": "sess-1", "tab_id": "tab-1",
                "snapshot_path": str(snapshot), "url": "https://www.linkedin.com/login",
                "session_scope": "browser_visible_dom"}, ws.path)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("密码输入框" in p for p in result["problems"]))

    def test_marker_anywhere_on_page_is_not_enough(self):
        """全页任意位置出现 My Network 不算：必须有账号区域链接与头部标志。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(state, ws)
            snapshot = _logged_in_snapshot(
                ws, state, url="https://www.linkedin.com/company/example/",
                text="About us\nMy Network appears in this marketing paragraph only",
                header_text="", anchors=[{"href": "/company/example/", "text": "About",
                                          "in_header": True}])
            result = workflow.record_action(state, {
                "action": "auth_probe", "channel": "linkedin",
                "browser_session_id": "sess-1", "tab_id": "tab-1",
                "snapshot_path": str(snapshot),
                "url": "https://www.linkedin.com/company/example/",
                "session_scope": "browser_visible_dom"}, ws.path)
            self.assertEqual(result["status"], "failed")
            joined = "；".join(result["problems"])
            self.assertIn("账号区域链接", joined)


class SessionBindingTests(unittest.TestCase):
    def _verified(self, ws, state):
        _open(state, ws)
        snapshot = _logged_in_snapshot(ws, state)
        workflow.record_action(state, {
            "action": "auth_probe", "channel": "linkedin", "browser_session_id": "sess-1",
            "tab_id": "tab-1", "snapshot_path": str(snapshot),
            "url": "https://www.linkedin.com/feed/",
            "session_scope": "browser_visible_dom"}, ws.path)
        self.assertEqual(workflow.auth_status(state, "linkedin"), "verified")

    def test_session_rebuild_invalidates_and_requires_reopen(self):
        """3. 浏览器重建后原验证立即失效，下一步动作要求重新打开/核验。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            self._verified(ws, state)
            state["browser"]["session_id"] = "sess-2"
            self.assertEqual(workflow.auth_status(state, "linkedin"), "invalidated")
            action = workflow.next_action(state)
            self.assertEqual(action["action"], "open_auth_tab")

    def test_tab_closed_invalidates_probe(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            self._verified(ws, state)
            snapshot = _logged_in_snapshot(ws, state, tab="tab-9")
            result = workflow.record_action(state, {
                "action": "auth_probe", "channel": "linkedin", "browser_session_id": "sess-1",
                "tab_id": "tab-9", "snapshot_path": str(snapshot),
                "url": "https://www.linkedin.com/feed/",
                "session_scope": "browser_visible_dom"}, ws.path)
            self.assertEqual(result["status"], "failed")
            joined = "；".join(result["problems"])
            self.assertIn("标签页", joined)
            self.assertNotEqual(workflow.auth_status(state, "linkedin"), "verified")

    def test_switching_account_invalidates(self):
        """换账号后页面标记不再匹配，下一次核验失败。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            self._verified(ws, state)
            snapshot = _logged_in_snapshot(
                ws, state, text="Sign in\nJoin now", header_text="Sign in Join now",
                anchors=[{"href": "/login", "text": "Sign in", "in_header": True}])
            result = workflow.record_action(state, {
                "action": "auth_probe", "channel": "linkedin", "browser_session_id": "sess-1",
                "tab_id": "tab-1", "snapshot_path": str(snapshot),
                "url": "https://www.linkedin.com/feed/",
                "session_scope": "browser_visible_dom"}, ws.path)
            self.assertEqual(result["status"], "failed")

    def test_business_search_blocked_without_verification(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            action = workflow.next_action(state)
            self.assertEqual(action["action"], "open_auth_tab")
            with self.assertRaises(ValueError) as ctx:
                workflow.record_action(state, helpers.search_receipt(
                    "l-1", channel="linkedin", exhausted=True), ws.path)
            self.assertIn("登录核验", str(ctx.exception))


class NoEmbeddedFallbackTests(unittest.TestCase):
    def test_capability_gap_instead_of_embedded_preview(self):
        """4. 要求外部浏览器但不可连接时，不回退内嵌预览。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            state["browser"] = {"backend": "external_browser", "status": "unavailable",
                                "reason": "本机未找到 Chrome/Edge", "installed": [],
                                "requires_local_connector": True,
                                "capabilities": {}, "detected_by": "filesystem+process+port"}
            action = workflow.next_action(state)
            self.assertEqual(action["action"], "capability_gap")
            self.assertTrue(action["requires_local_connector"])
            self.assertNotIn("login_url", action)

    def test_capability_report_ignores_host_claim(self):
        """能力报告必须来自实测；host 自述 available=true 不作数。"""
        with helpers.Workspace() as ws, \
                mock.patch.object(browser_backend, "find_browsers", return_value=[]):
            report = platform_auth.capability_report(ws.path, host={"visible_browser": True,
                                                                   "source": "模型自述"})
            self.assertEqual(report["status"], "unavailable")
            self.assertFalse(report["host_claim_accepted"])
            self.assertTrue(report["requires_local_connector"])
            self.assertIn("不接受自述", report["reason"])

    def test_embedded_surface_must_be_explicit(self):
        with helpers.Workspace() as ws:
            state = _state(ws, surface="embedded")
            self.assertEqual(state["scope"]["browser_surface"], "embedded")
            record = state["auth"].get("linkedin") or {}
            self.assertFalse(record.get("opened_event"))


if __name__ == "__main__":
    unittest.main()
