"""浏览器后端：默认离线校验参数；设置了 JOBFINDER_TEST_BROWSER=1 时做真实本机启动验收。"""
import os
import pathlib
import tempfile
import unittest

import helpers
from jobfinder import browser_backend


class BackendUnitTests(unittest.TestCase):
    def test_profile_dir_is_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = browser_backend.profile_dir(pathlib.Path(tmp) / "ws")
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.is_dir())

    def test_port_is_stable_per_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = browser_backend.profile_dir(tmp)
            self.assertEqual(browser_backend._pick_port(profile),
                             browser_backend._pick_port(profile))
            self.assertGreaterEqual(browser_backend._pick_port(profile), 9400)

    def test_status_reports_local_connector_flag_when_no_browser(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(browser_backend, "find_browsers", return_value=[]):
            report = browser_backend.capability_report(tmp)
            self.assertEqual(report["status"], "unavailable")
            self.assertTrue(report["requires_local_connector"])
            self.assertEqual(report["detected_by"], "filesystem+process+port")


@unittest.skipUnless(os.environ.get("JOBFINDER_TEST_BROWSER") == "1",
                     "真实浏览器验收需显式设置 JOBFINDER_TEST_BROWSER=1（会启动本机 Chrome/Edge）")
class RealBrowserSmokeTests(unittest.TestCase):
    def test_launch_open_snapshot_reuse_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = pathlib.Path(tmp) / "page.html"
            page.write_text("<html><head><title>JF</title></head><body>"
                            "<header><a href='/mynetwork/'>My Network</a></header>"
                            "</body></html>", encoding="utf-8")
            service = browser_backend.BrowserService(tmp, headless=True)
            record = service.ensure()
            handle = service.open(page.as_uri())
            snapshot = service.snapshot(handle["target_id"])
            self.assertEqual(snapshot["title"], "JF")
            self.assertTrue(any(a["href"] == "/mynetwork/" for a in snapshot["anchors"]))
            self.assertEqual(snapshot["session_id"], record["session_id"])
            second = browser_backend.BrowserService(tmp, headless=True).ensure()
            self.assertEqual(second["session_id"], record["session_id"])
            self.assertTrue(service.stop()["stopped"])


if __name__ == "__main__":
    unittest.main()
