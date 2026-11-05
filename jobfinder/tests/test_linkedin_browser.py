"""领英浏览器辅助检索：解析、边界、预算与停止条件（全部用假浏览器，不触网）。"""
import json
import unittest

import helpers
from jobfinder import linkedin, service, workflow


def _snapshot(url, *, text="", anchors=None, headings=None, json_ld=None, title="",
              session="sess-1", password_inputs=0):
    import hashlib
    return {
        "url": url, "title": title, "text": text, "anchors": anchors or [],
        "headings": headings or [], "json_ld": json_ld or [],
        "header_text": "", "password_inputs": password_inputs, "login_forms": 0,
        "has_captcha_frame": False, "retrieved_at": helpers.iso(),
        "content_hash": hashlib.sha256(text.encode()).hexdigest(), "session_id": session,
    }


SEARCH_TEXT = "搜索结果\n高级筛选\n财务BP\n北京"
SEARCH_ANCHORS = [
    {"href": "/jobs/view/1001/?refId=abc", "text": "Finance BP 甲公司"},
    {"href": "https://www.linkedin.com/jobs/view/1001/", "text": "Finance BP 甲公司"},
    {"href": "/jobs/view/1002/", "text": "财务分析经理 乙公司"},
    {"href": "/jobs/view/1003/", "text": "FP&A 丙公司"},
    {"href": "/jobs/search/?keywords=abc", "text": "搜索"},
]

JOB_TEXT_1001 = """Finance BP
甲公司 · 北京, 中国 · 3 天前 · 5 位申请者
Easy Apply
关于此职位
负责区域财务分析、预算编制与滚动预测，支持业务定价与毛利分析，推动经营改善。
任职要求
本科及以上学历，3 年以上财务分析经验，熟悉 Excel 与 BI 工具。
设置提醒
相似职位"""

JOB_LD_1002 = {
    "@type": "JobPosting",
    "title": "财务分析经理",
    "hiringOrganization": {"@type": "Organization", "name": "乙公司"},
    "jobLocation": {"@type": "Place", "address": {
        "@type": "PostalAddress", "addressLocality": "北京",
        "addressRegion": "北京市", "addressCountry": "CN"}},
    "description": "<p>负责月度经营分析、预算管理与成本控制，输出管理层报表。</p><ul><li>带团队</li></ul>",
    "employmentType": "FULL_TIME",
    "datePosted": "2026-10-30",
    "baseSalary": {"@type": "MonetaryAmount", "currency": "CNY",
                   "value": {"@type": "QuantitativeValue", "minValue": 30000,
                             "maxValue": 45000, "unitText": "MONTH"}},
}


class FakeBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.navigated = []
        self.current = None
        self.opened = 0

    def ensure(self):
        return {"session_id": "sess-1", "port": 9999}

    def has_target(self, tab_id):
        return tab_id == "tab-1"

    def open(self, url):
        self.opened += 1
        self.current = url
        return {"target_id": "tab-1", "session_id": "sess-1", "surface": "external_browser"}

    def navigate(self, tab_id, url, wait_seconds=20):
        self.navigated.append(url)
        self.current = url
        return {"target_id": tab_id, "url": url, "title": "page", "ready_state": "complete"}

    def snapshot(self, tab_id, wait_seconds=8):
        page = self.pages.get(self.current)
        if page is None:
            raise AssertionError("测试未准备该页面：" + str(self.current))
        return dict(page)

    def close_tab(self, tab_id):
        return {"closed": True}


def _pages_for_search():
    return {
        linkedin.search_url("财务BP", "北京"): _snapshot(
            linkedin.search_url("财务BP", "北京"), text=SEARCH_TEXT,
            anchors=SEARCH_ANCHORS, title="领英搜索"),
        linkedin.JOB_URL.format(job_id="1001"): _snapshot(
            linkedin.JOB_URL.format(job_id="1001"), text=JOB_TEXT_1001,
            headings=[{"tag": "h1", "text": "Finance BP"}], title="Finance BP | 领英"),
        linkedin.JOB_URL.format(job_id="1002"): _snapshot(
            linkedin.JOB_URL.format(job_id="1002"), text="财务分析经理\n乙公司 · 北京, 中国",
            headings=[{"tag": "h1", "text": "财务分析经理"}], json_ld=[JOB_LD_1002],
            title="财务分析经理 | 领英"),
    }


class ParserTests(unittest.TestCase):
    def test_search_url_encodes_keywords(self):
        url = linkedin.search_url("财务 BP", "北京")
        self.assertTrue(url.startswith("https://www.linkedin.com/jobs/search/?keywords="))
        self.assertIn("location=", url)
        self.assertNotIn(" ", url)

    def test_extract_job_links_dedupes_and_ignores_search(self):
        links = linkedin.extract_job_links(SEARCH_ANCHORS, limit=5)
        self.assertEqual([x["job_id"] for x in links], ["1001", "1002", "1003"])
        self.assertEqual(links[0]["url"], "https://www.linkedin.com/jobs/view/1001/")
        self.assertEqual(len(linkedin.extract_job_links(SEARCH_ANCHORS, limit=2)), 2)

    def test_extract_handles_current_job_id(self):
        links = linkedin.extract_job_links(
            [{"href": "/jobs/search/?currentJobId=7788", "text": "x"}], limit=3)
        self.assertEqual(links[0]["job_id"], "7788")

    def test_parse_json_ld_job(self):
        facts = linkedin.parse_job_snapshot(_snapshot(
            linkedin.JOB_URL.format(job_id="1002"), text="页面文字",
            json_ld=[JOB_LD_1002]))
        self.assertEqual(facts["title"], "财务分析经理")
        self.assertEqual(facts["company"], "乙公司")
        self.assertIn("北京", facts["location_raw"])
        self.assertIn("月度经营分析", facts["description"])
        self.assertEqual(facts["employment_type"], "FULL_TIME")
        self.assertEqual(facts["salary_raw"], "CNY 30000-45000 MONTH")
        self.assertEqual(facts["parse_source"], "json_ld")

    def test_parse_text_fallback(self):
        facts = linkedin.parse_job_snapshot(_snapshot(
            linkedin.JOB_URL.format(job_id="1001"), text=JOB_TEXT_1001,
            headings=[{"tag": "h1", "text": "Finance BP"}]))
        self.assertEqual(facts["title"], "Finance BP")
        self.assertEqual(facts["company"], "甲公司")
        self.assertEqual(facts["location_raw"], "北京, 中国")
        self.assertIn("预算编制", facts["description"])
        self.assertIn("本科及以上", facts["requirements"])
        self.assertEqual(facts["apply_type"], "easy_apply")
        self.assertIsNone(facts["salary_raw"])

    def test_text_page_without_core_fields_is_rejected(self):
        with self.assertRaises(linkedin.LinkedInAccessError):
            linkedin.parse_job_snapshot(_snapshot(
                linkedin.JOB_URL.format(job_id="1"), text="只有一句无关的话",
                headings=[{"tag": "h1", "text": "标题"}]))

    def test_guest_homepage_is_login_required_not_zero_jobs(self):
        """真实站点行为：未登录访问 /jobs/search 会被重定向到访客首页。"""
        with self.assertRaises(linkedin.LinkedInLoginRequired):
            linkedin.assert_not_blocked(_snapshot(
                "https://www.linkedin.com/", title="领英: 登录或注册",
                text="登录 | 马上加入 | 使用邮箱登录 | 您专属的职场社区"))
        with self.assertRaises(linkedin.LinkedInLoginRequired):
            linkedin.assert_not_blocked(_snapshot(
                "https://www.linkedin.com/jobs/search/?keywords=x",
                title="Jobs", text="Sign in with email", password_inputs=1))

    def test_checkpoint_page_stops(self):
        with self.assertRaises(linkedin.LinkedInCheckpoint):
            linkedin.assert_not_blocked(_snapshot(
                "https://www.linkedin.com/checkpoint/challenge/1", text="security check"))
        with self.assertRaises(linkedin.LinkedInCheckpoint):
            linkedin.assert_not_blocked(_snapshot(
                "https://www.linkedin.com/feed/", text="Verify you are human"))

    def test_job_from_browser_snapshot_marks_platform_source(self):
        job = linkedin.job_from_browser_snapshot(_snapshot(
            linkedin.JOB_URL.format(job_id="1001"), text=JOB_TEXT_1001,
            headings=[{"tag": "h1", "text": "Finance BP"}]))
        self.assertEqual(job.source_channel, "linkedin")
        self.assertEqual(job.source_type, "platform")
        self.assertEqual(job.evidence[0].method, "browser_visible_dom")
        self.assertEqual(job.apply_type, "easy_apply")
        self.assertTrue(job.details_complete)


class BrowserSearchFlowTests(unittest.TestCase):
    def setUp(self):
        # service.output_path 以模块级 ROOT 校验输出目录；测试里把它指到临时工作区。
        self._orig_root = service.ROOT

    def tearDown(self):
        service.ROOT = self._orig_root

    def _state(self, ws, *, linkedin_quota=5):
        service.ROOT = ws.path
        config = helpers.make_config(sources=["linkedin"], target_cities=["北京"],
                                     budget={"searches_per_city": 10,
                                             "per_channel": {"linkedin": linkedin_quota}})
        plan = helpers.make_plan(config, [helpers.make_task("li-1", channel="linkedin")],
                                 channels=["linkedin"])
        state = workflow.start_run(config, plan, ws.path, channels=["linkedin"],
                                   user_confirmed_scope=True, browser_surface="external")
        state["browser"] = {"backend": "external_browser", "status": "available",
                            "session_id": "sess-1", "installed": ["/fake/chrome"],
                            "capabilities": {}, "reason": None,
                            "requires_local_connector": False, "detected_by": "test"}
        workflow.record_action(state, {"action": "auth_open", "channel": "linkedin",
                                       "browser_session_id": "sess-1", "tab_id": "tab-1",
                                       "surface": "external_browser"}, ws.path)
        tab = ws.path / "data" / "runs" / state["run_id"] / "auth" / "snap.json"
        helpers.write_snapshot(
            tab, text="My Network\nMessaging", url="https://www.linkedin.com/feed/",
            header_text="My Network Messaging Notifications Me",
            anchors=[{"href": "/mynetwork/", "text": "My Network", "in_header": True},
                     {"href": "/messaging/", "text": "Messaging", "in_header": True},
                     {"href": "/me/", "text": "Me", "in_header": True}],
            target_id="tab-1", session_id="sess-1")
        workflow.record_action(state, {"action": "auth_probe", "channel": "linkedin",
                                       "browser_session_id": "sess-1", "browser_context_id": "sess-1",
                                       "tab_id": "tab-1", "snapshot_path": str(tab),
                                       "url": "https://www.linkedin.com/feed/",
                                       "session_scope": "browser_visible_dom"}, ws.path)
        self.assertEqual(workflow.auth_status(state, "linkedin"), "verified")
        return config, state

    def test_end_to_end_sample_reads_two_details(self):
        with helpers.Workspace() as ws:
            config, state = self._state(ws)
            browser = FakeBrowser(_pages_for_search())
            payload = service.run_linkedin_browser_search(
                config, ws.path / "outputs" / "real" / "li-search",
                keywords=["财务BP"], location="北京", run_id=state["run_id"],
                max_details=2, workspace=ws.path, browser=browser,
                user_confirmed=True, page_interval=0)
            self.assertEqual(len(payload["jobs"]), 2)
            self.assertEqual({j["source_channel"] for j in payload["jobs"]}, {"linkedin"})
            self.assertEqual({j["source_type"] for j in payload["jobs"]}, {"platform"})
            # 1 个搜索页 + 2 条详情
            self.assertEqual(len(browser.navigated), 3)
            self.assertEqual(payload["discovery"]["linkedin_browser_search"]["pages_read"], 3)
            state = workflow.load_state(ws.path, state["run_id"])
            self.assertNotIn("list_page", state["budget_used"].get("by_kind", {}))
            self.assertEqual(state["budget_used"]["by_kind"]["detail_read"]["linkedin"]["北京"], 2)
            self.assertEqual(state["budget_used"]["searches"]["北京"], 1)
            self.assertTrue(any(e["kind"] == "browser_search" for e in state["events"]))
            self.assertIn("小样本", "；".join(payload["discovery"]["completion"]["reasons"]))
            # 保留原图快照与十列汇总
            files = {p.name for p in (ws.path / "outputs" / "real" / "li-search").iterdir()}
            self.assertIn("results.json", files)
            self.assertTrue(any(name.startswith("岗位汇总") for name in files))

    def test_requires_verified_login(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(sources=["linkedin"], target_cities=["北京"])
            plan = helpers.make_plan(config, [helpers.make_task("li-1", channel="linkedin")],
                                     channels=["linkedin"])
            state = workflow.start_run(config, plan, ws.path, channels=["linkedin"],
                                       user_confirmed_scope=True, browser_surface="external")
            service.ROOT = ws.path
            with self.assertRaises(ValueError) as ctx:
                service.run_linkedin_browser_search(
                    config, ws.path / "outputs" / "real" / "x", keywords=["财务BP"],
                    location="北京", run_id=state["run_id"], workspace=ws.path,
                    browser=FakeBrowser(_pages_for_search()), user_confirmed=True,
                    page_interval=0)
            self.assertIn("核验", str(ctx.exception))

    def test_requires_user_confirmation(self):
        with helpers.Workspace() as ws:
            config, state = self._state(ws)
            with self.assertRaises(ValueError) as ctx:
                service.run_linkedin_browser_search(
                    config, ws.path / "outputs" / "real" / "x", keywords=["财务BP"],
                    location="北京", run_id=state["run_id"], workspace=ws.path,
                    browser=FakeBrowser(_pages_for_search()), user_confirmed=False,
                    page_interval=0)
            self.assertIn("用户明确同意", str(ctx.exception))

    def test_detail_limit_is_capped(self):
        with helpers.Workspace() as ws:
            config, state = self._state(ws)
            with self.assertRaises(ValueError):
                service.run_linkedin_browser_search(
                    config, ws.path / "outputs" / "real" / "x", keywords=["财务BP"],
                    location="北京", run_id=state["run_id"], workspace=ws.path,
                    browser=FakeBrowser(_pages_for_search()), user_confirmed=True,
                    max_details=9, page_interval=0)

    def test_location_must_be_target_city(self):
        with helpers.Workspace() as ws:
            config, state = self._state(ws)
            with self.assertRaises(ValueError):
                service.run_linkedin_browser_search(
                    config, ws.path / "outputs" / "real" / "x", keywords=["财务BP"],
                    location="上海", run_id=state["run_id"], workspace=ws.path,
                    browser=FakeBrowser(_pages_for_search()), user_confirmed=True,
                    page_interval=0)

    def test_budget_shortage_stops_details_and_records_gap(self):
        with helpers.Workspace() as ws:
            config, state = self._state(ws, linkedin_quota=1)   # 只有 1 次：搜索用掉后无详情额度
            browser = FakeBrowser(_pages_for_search())
            payload = service.run_linkedin_browser_search(
                config, ws.path / "outputs" / "real" / "li-quota", keywords=["财务BP"],
                location="北京", run_id=state["run_id"], max_details=2,
                workspace=ws.path, browser=browser, user_confirmed=True, page_interval=0)
            self.assertEqual(len(payload["jobs"]), 0)
            summary = payload["discovery"]["linkedin_browser_search"]
            self.assertTrue(summary["problems"])
            self.assertIn("预算不足", summary["problems"][0]["reason"])
            # 搜索页读了，但没有继续打开详情
            self.assertEqual(len(browser.navigated), 1)

    def test_login_wall_reports_login_required_and_invalidates(self):
        with helpers.Workspace() as ws:
            config, state = self._state(ws)
            url = linkedin.search_url("财务BP", "北京")
            browser = FakeBrowser({url: _snapshot(
                "https://www.linkedin.com/", title="领英: 登录或注册",
                text="登录 | 马上加入 | 使用邮箱登录")})
            payload = service.run_linkedin_browser_search(
                config, ws.path / "outputs" / "real" / "li-login", keywords=["财务BP"],
                location="北京", run_id=state["run_id"], workspace=ws.path,
                browser=browser, user_confirmed=True, page_interval=0)
            summary = payload["discovery"]["linkedin_browser_search"]
            self.assertTrue(summary["login_required"])
            self.assertIsNone(summary["checkpoint"])
            self.assertEqual(summary["jobs_found"], 0)
            self.assertEqual(payload["reports"][0]["access_status"], "login_required")
            self.assertNotIn("没有岗位", payload["reports"][0]["reason"])
            reloaded = workflow.load_state(ws.path, state["run_id"])
            self.assertEqual(workflow.auth_status(reloaded, "linkedin"), "invalidated")

    def test_checkpoint_stops_and_sets_cooldown(self):
        with helpers.Workspace() as ws:
            config, state = self._state(ws)
            url = linkedin.search_url("财务BP", "北京")
            browser = FakeBrowser({url: _snapshot(
                "https://www.linkedin.com/checkpoint/challenge/9",
                text="Security check, verify you are human")})
            payload = service.run_linkedin_browser_search(
                config, ws.path / "outputs" / "real" / "li-block", keywords=["财务BP"],
                location="北京", run_id=state["run_id"], workspace=ws.path,
                browser=browser, user_confirmed=True, page_interval=0)
            self.assertEqual(len(payload["jobs"]), 0)
            self.assertTrue(payload["discovery"]["linkedin_browser_search"]["checkpoint"])
            from jobfinder import access_control
            view = access_control.cooldown(ws.path, "linkedin")
            self.assertIsNotNone(view)
            self.assertEqual(view["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
