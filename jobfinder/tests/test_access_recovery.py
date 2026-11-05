"""阶段三验收：平台串行锁、分动作计量、跨 run 状态、缓存复用。"""
import json
import os
import unittest

import helpers
from jobfinder import access_control, budgets, service, workflow
from jobfinder.models import Candidate, Job


def _candidate(**overrides):
    data = dict(query="官网 北京 财务BP", url="https://example.com/jobs/1",
                title="财务BP", discovered_at=helpers.iso(), source_channel="official_career",
                company="示例公司", target_cities=["北京"], candidate_type="career_entry")
    data.update(overrides)
    return Candidate.model_validate(data)


class PlatformLockTests(unittest.TestCase):
    def test_second_worker_is_rejected(self):
        with helpers.Workspace() as ws:
            lock = access_control.platform_lock(ws.path, "zhaopin")
            lock.acquire()
            try:
                with self.assertRaises(access_control.PlatformBusy) as ctx:
                    access_control.platform_lock(ws.path, "zhaopin").acquire()
                self.assertIn("串行", str(ctx.exception))
                status = access_control.lock_status(ws.path)
                self.assertEqual(len(status), 1)
                self.assertTrue(status[0]["held"])
            finally:
                lock.release()
            access_control.platform_lock(ws.path, "zhaopin").acquire()

    def test_stale_lock_from_dead_process_is_reclaimed(self):
        with helpers.Workspace() as ws:
            path = ws.path / "data" / "locks" / "zhaopin.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"channel": "zhaopin", "pid": 999999,
                                        "acquired_at": helpers.iso(600)}), encoding="utf-8")
            with access_control.platform_lock(ws.path, "zhaopin") as held:
                self.assertEqual(held["pid"], os.getpid())

    def test_context_manager_releases(self):
        with helpers.Workspace() as ws:
            with access_control.platform_lock(ws.path, "51job"):
                self.assertTrue(access_control.lock_status(ws.path)[0]["held"])
            self.assertEqual(access_control.lock_status(ws.path), [])


class PlatformStateTests(unittest.TestCase):
    def test_rate_limit_sets_cooldown_and_success_clears_state(self):
        with helpers.Workspace() as ws:
            access_control.note_restricted(ws.path, "zhaopin", kind="rate_limited",
                                           reason="429", retry_after=60)
            self.assertEqual(access_control.cooldown(ws.path, "zhaopin")["state"], "cooling")
            access_control.note_success(ws.path, "zhaopin")
            self.assertIsNone(access_control.cooldown(ws.path, "zhaopin"))

    def test_explicit_rejection_blocks_until_user_retry(self):
        with helpers.Workspace() as ws:
            access_control.note_restricted(ws.path, "boss", kind="restricted",
                                           reason="账号被限制", retry_after=10)
            self.assertEqual(access_control.cooldown(ws.path, "boss")["state"], "blocked")
            access_control.clear_cooldown(ws.path, "boss", user_confirmed=True)
            self.assertIsNone(access_control.cooldown(ws.path, "boss"))


class PageCacheTests(unittest.TestCase):
    def test_cache_key_covers_source_identity_context_and_account(self):
        base = access_control.cache_key("zhaopin", "job-1",
                                        query_context={"query": "财务BP", "city": "北京"},
                                        account_scope="anonymous")
        other_query = access_control.cache_key("zhaopin", "job-1",
                                               query_context={"query": "HR", "city": "北京"},
                                               account_scope="anonymous")
        other_city = access_control.cache_key("zhaopin", "job-1",
                                              query_context={"query": "财务BP", "city": "深圳"},
                                              account_scope="anonymous")
        other_account = access_control.cache_key("zhaopin", "job-1",
                                                 query_context={"query": "财务BP", "city": "北京"},
                                                 account_scope="browser_session")
        other_channel = access_control.cache_key("51job", "job-1",
                                                 query_context={"query": "财务BP", "city": "北京"},
                                                 account_scope="anonymous")
        self.assertEqual(len({base, other_query, other_city, other_account, other_channel}), 5)

    def test_store_and_reuse_keeps_original_evidence_time(self):
        with helpers.Workspace() as ws:
            candidate = _candidate()
            job = Job(company="示例公司", title="财务BP", description="职责" * 30,
                      source_url=candidate.url, source_channel="official_career",
                      location_raw="北京", locations=["北京"])
            service.store_cached_page(candidate, [job], ws.path, "2026-10-01T09:00:00+08:00")
            hit = service.reuse_cached_page(candidate, ws.path)
            self.assertIsNotNone(hit)
            self.assertEqual(len(hit["jobs"]), 1)
            self.assertIn("原读取时间", hit["reason"])
            self.assertEqual(access_control.cache_stats(ws.path)["entries"], 1)

    def test_cache_is_not_shared_across_queries(self):
        with helpers.Workspace() as ws:
            candidate = _candidate()
            job = Job(company="示例公司", title="财务BP", description="职责" * 30,
                      source_url=candidate.url, source_channel="official_career")
            service.store_cached_page(candidate, [job], ws.path)
            other = _candidate(query="官网 深圳 财务BP", target_cities=["深圳"])
            self.assertIsNone(service.reuse_cached_page(other, ws.path))

    def test_local_file_candidate_never_uses_cache(self):
        with helpers.Workspace() as ws:
            self.assertIsNone(service.reuse_cached_page(
                _candidate(local_file="a.html", authorized_import=True), ws.path))


class ActionMeteringTests(unittest.TestCase):
    def test_each_kind_is_metered_separately(self):
        with helpers.Workspace() as ws:
            config = helpers.make_config(budget={"searches_per_city": 10,
                                                 "per_channel": {"zhaopin": 8}})
            tasks = [helpers.make_task("s-1"), helpers.make_task("p-1", route="browse_list",
                                                                 browse_url="https://x/1")]
            plan = helpers.make_plan(config, tasks, channels=["zhaopin"])
            state = workflow.start_run(config, plan, ws.path, channels=["zhaopin"],
                                       public_only=True, user_confirmed_scope=True)
            workflow.record_action(state, helpers.search_receipt("s-1", exhausted=True), ws.path)
            workflow.record_action(state, helpers.search_receipt("p-1", exhausted=True), ws.path)
            self.assertEqual(budgets.used(state, "zhaopin", "北京", budgets.SEARCH), 2)
            self.assertEqual(budgets.used(state, "zhaopin", "北京", budgets.LIST_PAGE), 1)
            self.assertEqual(budgets.used(state, "zhaopin", "北京", budgets.DETAIL_READ), 0)
            self.assertEqual(state["budget_used"]["searches"]["北京"], 2)
            budgets.note_http(state, "北京", 3)
            self.assertEqual(state["budget_used"]["http_requests"]["北京"], 3)


if __name__ == "__main__":
    unittest.main()
