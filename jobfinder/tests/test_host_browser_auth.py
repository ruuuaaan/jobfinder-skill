"""宿主机浏览器通道：平台渠道统一走"浏览器打开登录 → 工具输入搜索 → 读可见 DOM"。

这条通道没有本机 BrowserService 可探测，绑定强度靠 browser_id + tab_id + 快照新鲜度，
因此测试重点在：换浏览器/换标签页/登录控件仍在/账号入口缺失时必须判失败。
"""
import unittest

import helpers
from jobfinder import platform_auth, workflow


def _state(ws, *, channel="51job"):
    config = helpers.make_config(sources=[channel])
    plan = helpers.make_plan(config, [helpers.make_task("h-1", channel=channel)],
                             channels=[channel])
    state = workflow.start_run(config, plan, ws.path, channels=[channel],
                               user_confirmed_scope=True, browser_surface="external")
    return state


def _open(ws, state, *, channel="51job", browser_id=2, tab_id=1,
          url="https://login.51job.com/login.php"):
    workflow.record_action(state, {"action": "auth_open", "channel": channel,
                                   "browser_session_id": f"host:{browser_id}",
                                   "browser_context_id": f"host:{browser_id}",
                                   "tab_id": str(tab_id), "surface": "external_browser",
                                   "backend": "host_browser", "browser_id": browser_id,
                                   "login_url": url}, ws.path)


LOGGED_IN_DOM = """<a node_id=1 href="https://www.51job.com">首页</a>
<a node_id=2 href="https://we.51job.com/pc/my/myjob">我的51job</a>
<a node_id=3 href="https://i.51job.com/userset/my_apply.php">投递记录</a>
<a node_id=4 href="https://i.51job.com/userset/security_center.php">账号设置</a>
<input node_id=5 placeholder="搜索职位" type="text" value="财务BP" />"""

LOGGED_OUT_DOM = """<a node_id=1 href="https://www.51job.com">首页</a>
<a node_id=2 href="https://login.51job.com/login.php">登录/注册</a>
<input node_id=3 type="password" />"""


def _probe(ws, state, dom, *, channel="51job", browser_id=2, tab_id=1,
           url="https://we.51job.com/pc/my/myjob", title="我的51Job"):
    snap = platform_auth.build_host_snapshot(dom, url=url, title=title,
                                             browser_id=browser_id, tab_id=tab_id)
    path = workflow.state_file(ws.path, state["run_id"]).parent / "auth" / "host.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return workflow.record_action(state, {
        "action": "auth_probe", "channel": channel, "backend": "host_browser",
        "browser_id": browser_id, "tab_id": str(tab_id),
        "snapshot_path": str(path), "url": url,
        "session_scope": "browser_visible_dom"}, ws.path)


class HostBrowserAuthTests(unittest.TestCase):
    def test_logged_in_dom_verifies(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            result = _probe(ws, state, LOGGED_IN_DOM)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(workflow.auth_status(state, "51job"), "verified")
            self.assertEqual(state["auth"]["51job"]["backend"], "host_browser")

    def test_login_control_still_present_is_rejected(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            result = _probe(ws, state, LOGGED_OUT_DOM, url="https://we.51job.com/")
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("登录入口" in p or "密码输入框" in p for p in result["problems"]))

    def test_other_tab_is_rejected(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state, tab_id=1)
            result = _probe(ws, state, LOGGED_IN_DOM, tab_id=9)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("标签页" in p for p in result["problems"]))

    def test_other_browser_instance_is_rejected(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state, browser_id=2)
            result = _probe(ws, state, LOGGED_IN_DOM, browser_id=1)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("browser_id" in p for p in result["problems"]))

    def test_dom_without_account_entry_is_rejected(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            bare = '<a href="https://www.51job.com/">首页</a>\n<a href="https://news.51job.com/">资讯</a>'
            result = _probe(ws, state, bare)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("账号入口" in p for p in result["problems"]))

    def test_login_url_path_is_rejected_even_if_dom_looks_right(self):
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            result = _probe(ws, state, LOGGED_IN_DOM, url="https://login.51job.com/login.php")
            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("登录/风控页面" in p for p in result["problems"]))

    def test_host_mode_skips_dedicated_browser_liveness_check(self):
        """宿主通道没有本机 BrowserService，不能因此判"浏览器已关闭"。"""
        with helpers.Workspace() as ws:
            state = _state(ws)
            _open(ws, state)
            result = _probe(ws, state, LOGGED_IN_DOM)
            self.assertFalse(any("浏览器已关闭" in p for p in result["problems"]))


if __name__ == "__main__":
    unittest.main()
