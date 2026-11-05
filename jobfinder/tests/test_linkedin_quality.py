"""验收 13/14/15/16：领英链路一致、英文学历与同义词、阶段性导出与报告条件。"""
import json
import unittest
from pathlib import Path

import helpers
from jobfinder import channels, education, exporter, linkedin, matching, planning, search_budget, workflow
from jobfinder.browser import condition_line, write_report
from jobfinder.models import Evidence, Job


class LinkedInChannelTests(unittest.TestCase):
    def test_channel_registry_is_consistent(self):
        """13. 领英在菜单、全选、规划、来源和导出中一致。"""
        menu = channels.menu()
        names = [c["channel"] for row in menu for c in row["channels"]]
        self.assertIn("linkedin", names)
        self.assertIn("linkedin", channels.SUPPORTED)
        picked, all_flag = channels.resolve_selection(None, all_channels=True)
        self.assertTrue(all_flag)
        self.assertIn("linkedin", picked)
        self.assertEqual(workflow.CHANNEL_STAGE["linkedin"], "recruitment")
        self.assertIn("linkedin", workflow.PLATFORM_CHANNELS)
        self.assertEqual(workflow.login_url("linkedin"), "https://www.linkedin.com/login")
        self.assertEqual(planning.CHANNEL_METHODS["linkedin"][0], "领英")
        self.assertEqual(exporter.source_label("linkedin"), "领英")
        self.assertEqual(channels.default_quota("linkedin"), 5)

    def test_planning_creates_linkedin_tasks_with_accessible_mode(self):
        config = helpers.make_config(sources=["official_career", "linkedin"],
                                     target_cities=["北京"])
        plan = planning.build_plan(config)
        rows = [t for t in plan["tasks"] if t["channel"] == "linkedin"]
        self.assertTrue(rows)
        self.assertTrue(all(t["stage"] == "recruitment" for t in rows))
        self.assertTrue(all("领英" in t["query"] for t in rows))
        self.assertEqual(channels.access_mode("linkedin"), "assisted_manual")
        self.assertFalse(channels.automation_allowed("linkedin"))
        access = plan["channel_access"]["linkedin"]
        self.assertEqual(access["mode"], "assisted_manual")
        self.assertFalse(access["automation_allowed"])
        self.assertTrue(access["login_required"])
        profile = linkedin.profile_from_plan(plan)
        self.assertFalse(profile["automation_enabled"])
        self.assertTrue(profile["manual_path_available"])

    def test_linkedin_quota_does_not_squeeze_official_silently(self):
        """新增领英后总预算与分渠道预算一致；不足时报错而不是挤占官网。"""
        cities = ["北京"]
        with_li = ["official_career", "51job", "zhaopin", "boss", "liepin", "linkedin"]
        allocation = search_budget.base_allocation(cities, with_li, {"北京": 40},
                                                   {"51job": 5, "zhaopin": 5, "boss": 5,
                                                    "liepin": 5, "linkedin": 5})
        self.assertEqual(allocation["北京"]["official_career"], 40 - 25)
        self.assertEqual(allocation["北京"]["linkedin"], 5)
        self.assertEqual(sum(allocation["北京"].values()), 40)
        with self.assertRaises(ValueError):
            search_budget.validate_allocation(cities, with_li, {"北京": 20},
                                              {"51job": 5, "zhaopin": 5, "boss": 5,
                                               "liepin": 5, "linkedin": 5},
                                              search_budget.PRIORITY)

    def test_access_mode_distinguishes_login_from_license(self):
        profile = linkedin.access_profile()
        self.assertTrue(profile["manual_path_available"])
        self.assertFalse(profile["automation_enabled"])
        self.assertFalse(profile["capabilities"]["auto_listing_traversal"])
        self.assertFalse(profile["capabilities"]["job_posting_api_as_search"])
        with self.assertRaises(linkedin.LinkedInAccessError):
            linkedin.access_profile("licensed_api")
        licensed = linkedin.access_profile("licensed_api",
                                           {"evidence_url": "https://example.com/contract"})
        self.assertTrue(licensed["automation_enabled"])

    def test_material_job_uses_platform_source_and_keeps_unknowns(self):
        record = {"authorized": True, "url": "https://www.linkedin.com/jobs/view/123",
                  "company": "Example Ltd", "title": "Finance Business Partner",
                  "description": "Own the P&L for the region and partner with sales.",
                  "requirements": "Bachelor's degree preferred",
                  "location_raw": "Beijing, China", "apply_type": "easy_apply"}
        job = linkedin.job_from_material(record, source_ref="material.json")
        self.assertEqual(job.source_channel, "linkedin")
        self.assertEqual(job.source_type, "platform")
        self.assertIsNone(job.salary_raw)
        self.assertEqual(job.apply_type, "easy_apply")
        self.assertEqual(job.evidence[0].method, "user_provided_material")

    def test_material_rejects_unauthorized_or_foreign_host(self):
        base = {"authorized": True, "url": "https://www.linkedin.com/jobs/view/1",
                "company": "A", "title": "B", "description": "C"}
        with self.assertRaises(linkedin.LinkedInAccessError):
            linkedin.material_jobs({"jobs": [dict(base, authorized=False)]})
        with self.assertRaises(linkedin.LinkedInAccessError):
            linkedin.material_jobs({"jobs": [dict(base, url="https://example.com/job/1")]})

    def test_china_only_location_is_not_filled_with_a_city(self):
        record = {"authorized": True, "url": "https://www.linkedin.com/jobs/view/2",
                  "company": "A", "title": "B", "description": "C" * 20,
                  "location_raw": "China"}
        job = linkedin.job_from_material(record)
        self.assertEqual(job.location_raw, "China")
        config = helpers.make_config(target_cities=["北京"])
        result = matching.evaluate(job, config)[0]
        self.assertEqual(result.geo.state, "UNKNOWN")
        self.assertNotEqual(result.status, "MATCH")


class EnglishMatchingTests(unittest.TestCase):
    def test_degree_preferences_and_equivalents(self):
        """14. 英文学历优先项、等价经验不被硬判。"""
        cases = {
            "Master's degree preferred": education.EducationLevel.UNKNOWN,
            "Bachelor's degree or equivalent experience": education.EducationLevel.UNKNOWN,
            "Bachelor's degree required, Master's preferred": education.EducationLevel.BACHELOR,
            "Bachelor's degree required": education.EducationLevel.BACHELOR,
            "Master's degree": education.EducationLevel.MASTER,
        }
        for text, expected in cases.items():
            fact = education.normalize_education(text)
            self.assertEqual(fact.minimum, expected, text)

    def test_master_preferred_does_not_fail_bachelor_candidate(self):
        state, reason = education.compare_candidate("Master's degree preferred",
                                                    education.EducationLevel.BACHELOR)
        self.assertEqual(state, "UNKNOWN")
        self.assertIn("优先", reason)

    def test_equivalent_experience_goes_to_review(self):
        state, reason = education.compare_candidate(
            "Bachelor's degree or equivalent experience", education.EducationLevel.ASSOCIATE)
        self.assertEqual(state, "UNKNOWN")

    def test_english_synonym_matches(self):
        decision = matching.role_match("Finance Business Partner - North China", ["财务BP"])
        self.assertEqual(decision.state, "PASS")
        self.assertIn("同义", decision.reason)

    def test_unlisted_english_title_is_not_hard_excluded(self):
        decision = matching.role_match("Senior Accountant", ["财务BP"])
        self.assertEqual(decision.state, "UNKNOWN")

    def test_chinese_title_still_fails(self):
        decision = matching.role_match("会计", ["财务BP"])
        self.assertEqual(decision.state, "FAIL")

    def test_plan_carries_synonyms_for_search(self):
        config = helpers.make_config(target_cities=["北京"], role_keywords=["财务BP"],
                                     sources=["zhaopin"])
        plan = planning.build_plan(config)
        self.assertIn("财务BP", plan["synonym_expansions"])
        self.assertIn("finance bp", plan["synonym_expansions"]["财务BP"])
        self.assertTrue(all(t.get("synonyms") for t in plan["tasks"]
                            if t["channel"] == "zhaopin"))


def _job(**overrides):
    data = dict(company="北京示例科技有限公司", title="财务BP", description="负责区域财务分析与业务支持，"
                "编制预算与滚动预测，参与定价与毛利分析，推动经营改善。" * 2,
                requirements="本科及以上学历，3年以上财务分析经验" * 2,
                source_url="https://jobs.example.com/job/1", detail_url="https://jobs.example.com/job/1",
                source_channel="zhaopin", source_type="platform", location_raw="北京",
                locations=["北京"], education_raw="本科及以上", experience_raw="3年以上",
                open_status="OPEN", details_complete=True, evidence_level="original_detail",
                application_url="https://jobs.example.com/apply/1",
                application_method="站内投递入口，未点击",
                evidence=[Evidence(source_url="https://jobs.example.com/job/1",
                                   content_hash="abc", method="browser_visible_dom",
                                   field_quotes={"company": "北京示例科技有限公司",
                                                 "title": "财务BP"})])
    data.update(overrides)
    return Job(**data)


class StagedExportTests(unittest.TestCase):
    def _export(self, ws, results, completion):
        config = helpers.make_config(target_cities=["北京"], sources=["zhaopin"])
        discovery = {"provider": "test", "status": "partial", "operation": "new_search",
                     "coverage": "测试范围", "search_log": [], "candidates": [],
                     "completion": completion}
        payload = exporter.export_bundle(ws.path / "out", config, [r.job for r in results],
                                         results, [], [], discovery, events=[], mode="test")
        return payload

    def test_partial_summary_is_labelled_and_keeps_match_pending(self):
        """15. 阶段性主表能看出未完成；MATCH 的待确认说明不丢失。"""
        config = helpers.make_config(target_cities=["北京"], sources=["zhaopin"])
        result = matching.evaluate(_job(), config)[0]
        self.assertEqual(result.status, "MATCH")
        completion = {"run_id": "run-abc123", "protocol_version": "workflow-1.2",
                      "verdict": "PARTIAL", "partial_export": True,
                      "reasons": ["存在预算缺口的渠道：zhaopin(0受阻,0跳过,1预算)"],
                      "delivery_blockers": ["搜索范围未到底"],
                      "scopes": [{"channel": "zhaopin", "city": "北京",
                                  "query_scope": "财务bp", "exhausted": False}],
                      "stage_order": ["recruitment"], "channels": [], "stages": [],
                      "next_action": {"action": "continue_channel"}, "boundary": "边界"}
        with helpers.Workspace() as ws:
            payload = self._export(ws, [result], completion)
            name = payload["discovery"]["summary_file"]
            self.assertIn("阶段性", name)
            self.assertIn("run-abc123", name)
            from openpyxl import load_workbook
            workbook = load_workbook(ws.path / "out" / name)
            self.assertIn("导出说明", workbook.sheetnames)
            sheet = workbook["岗位汇总"]
            self.assertEqual(sheet.max_column, 10)
            note = sheet.cell(row=2, column=10).value
            self.assertIn("未确认事项", note)
            self.assertIn("投递入口", note)
            note_sheet = {r[0].value: r[1].value for r in workbook["导出说明"].iter_rows(min_row=2)}
            self.assertEqual(note_sheet["运行 ID"], "run-abc123")
            self.assertIn("阶段性", note_sheet["导出性质"])

    def test_results_workbook_has_separate_status_columns(self):
        config = helpers.make_config(target_cities=["北京"], sources=["zhaopin"])
        result = matching.evaluate(_job(), config)[0]
        completion = {"run_id": "run-x", "verdict": "COMPLETE", "partial_export": False,
                      "reasons": [], "stage_order": ["recruitment"], "channels": [],
                      "stages": [], "boundary": "b"}
        with helpers.Workspace() as ws:
            self._export(ws, [result], completion)
            from openpyxl import load_workbook
            workbook = load_workbook(ws.path / "out" / "results.xlsx")
            headers = [c.value for c in workbook["匹配岗位"][1]]
            self.assertIn("适配结论", headers)
            self.assertIn("证据等级", headers)
            self.assertIn("投递状态", headers)

    def test_report_conditions_come_from_config(self):
        """16. 不同查询条件生成的报告展示各自真实条件。"""
        first = helpers.make_config(target_cities=["北京"], role_keywords=["财务BP"],
                                    sources=["zhaopin"])
        second = helpers.make_config(target_cities=["深圳"], role_keywords=["软件测试"],
                                     sources=["zhaopin"])
        self.assertIn("北京", condition_line(first))
        self.assertIn("财务BP", condition_line(first))
        self.assertIn("深圳", condition_line(second))
        self.assertIn("软件测试", condition_line(second))
        self.assertNotEqual(condition_line(first), condition_line(second))

        payload = {"reports": [{"company": "甲公司", "listed_count": 3, "detail_count": 2,
                                "full_detail_count": 2, "coverage_complete": True,
                                "scope": "声明范围", "reason": "无"}],
                   "results": [], "jobs": [], "counts": {"MATCH": 0, "REVIEW": 0, "EXCLUDED": 0},
                   "generated_at": "2026-11-02T20:00:00+08:00",
                   "discovery": {"summary_file": "岗位汇总_阶段性_run-1.xlsx"}}
        receipt = {"events": [], "snapshots": []}
        with helpers.Workspace() as ws:
            out = ws.path / "out"
            out.mkdir()
            write_report(out, payload, receipt, second)
            page = (out / "report.html").read_text(encoding="utf-8")
            self.assertIn("深圳", page)
            self.assertIn("软件测试", page)
            self.assertIn("1 家企业", page)
            self.assertIn("10 列表格", page)
            for hardcoded in ("四家企业", "九列表格", "广州 · 财务 BP"):
                self.assertNotIn(hardcoded, page)


if __name__ == "__main__":
    unittest.main()
