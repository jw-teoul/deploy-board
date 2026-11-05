"""화면 보안 헤더·웹 로그인 설정·azure 병합 테스트(az·gh·네트워크 호출 없음, 127.0.0.1 임시 포트만). python -m unittest test_server"""
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import collector
import server

ENTRA = {"tenant_id": "t", "client_id": "c"}


class Headers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, b""

    def test_index_every_path_has_page_csp(self):  # index.html 로 풀리는 경로는 모두 화면 CSP(인라인 해시) — 검토 블로커 1
        paths = ["/", "/index.html", "/./index.html", "/%69ndex.html", "/vendor/../index.html", "/index.html?x=1"]
        if os.name == "nt":
            paths.append("/Index.html")
        for p in paths:
            for m in ("GET", "HEAD"):
                code, h, body = self.get(p, m)
                self.assertEqual(code, 200, (p, m))
                self.assertIn("script-src 'self' 'sha256-", h.get("Content-Security-Policy", ""), (p, m))
                self.assertEqual(h.get("Referrer-Policy"), "no-referrer", (p, m))
                if m == "GET":
                    self.assertIn(b"weblogin.js?v=", body, p)

    def test_other_files_strict_csp(self):
        for p in ("/weblogin.js", "/pixel.js", "/vendor/msal-browser.min.js"):
            code, h, _ = self.get(p)
            self.assertEqual(code, 200, p)
            self.assertIn("default-src 'none'", h.get("Content-Security-Policy", ""), p)

    def test_no_directory_listing(self):
        for p in ("/vendor/", "/assets/"):
            code, h, _ = self.get(p)
            self.assertEqual(code, 404, p)

    def test_inline_hash_matches(self):
        html = server.render_index()
        policy = server.csp(html)
        inline = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(inline)
        self.assertEqual(policy.count("'sha256-"), len(inline))
        self.assertNotIn("'unsafe-inline'", policy.split("script-src", 1)[1].split(";", 1)[0])
        self.assertIn("connect-src 'self' https://login.microsoftonline.com https://management.azure.com;", policy)


class WebLogin(unittest.TestCase):
    def setUp(self):
        self.mode = server.status["mode"]

    def tearDown(self):
        server.status["mode"] = self.mode

    def test_off_in_swap_mode(self):
        server.status["mode"] = "swap"
        self.assertIsNone(server.web_login({"entra": ENTRA}))

    def test_on_otherwise(self):
        for m in ("local", "readonly"):
            server.status["mode"] = m
            w = server.web_login({"entra": ENTRA})
            self.assertEqual((w["tenant_id"], w["client_id"]), ("t", "c"))
            self.assertTrue(w["msal"].startswith("vendor/msal-browser.min.js?v="))

    def test_missing_config(self):
        server.status["mode"] = "readonly"
        self.assertIsNone(server.web_login({}))
        self.assertIsNone(server.web_login({"entra": {"tenant_id": "t"}}))


class MergeAzure(unittest.TestCase):
    def merge(self, old, new):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(old, f)
            return collector.merge_state(new, p)

    def test_keeps_and_replaces(self):
        az = {"subscription_id": "s", "resource_group": "g"}
        base = {"repos": [], "collected_at": "a", "duration_s": 1, "errors": []}
        self.assertEqual(self.merge({**base, "azure": az}, dict(base))["azure"], az)   # 새 결과에 없으면 유지
        az2 = {"subscription_id": "s2", "resource_group": "g"}
        self.assertEqual(self.merge({**base, "azure": az}, {**base, "azure": az2})["azure"], az2)


class PublishRetryTest(unittest.TestCase):
    def setUp(self):
        self.orig = (server.publish.publish, server.OPT.get("publish"), server.status.get("publish"), server.status["running"])
        self.results = []
        server.publish.publish = lambda target, src: self.results.pop(0)
        server.OPT["publish"] = "host:x/state.json"
        server.status["publish"] = {"target": "host:x/state.json", "last": None, "ok_at": None, "error": "ssh 실패"}
        server.status["running"] = False

    def tearDown(self):
        server.publish.publish, server.OPT["publish"], server.status["publish"], server.status["running"] = self.orig

    def test_retries_with_backoff_and_stops_on_success(self):
        r = server.PublishRetry()
        self.assertFalse(r.step(0))            # 1분 뒤로 예약
        self.assertFalse(r.step(59))
        self.results = ["또 실패"]; self.assertTrue(r.step(60))
        self.assertEqual(server.status["publish"]["error"], "또 실패")
        self.assertFalse(r.step(61)); self.assertFalse(r.step(61 + 119))   # 두 번째는 2분 뒤
        server.status["running"] = True; self.assertFalse(r.step(61 + 120))   # 수집 중이면 미룸
        server.status["running"] = False
        self.results = [None]; self.assertTrue(r.step(61 + 121))
        self.assertIsNone(server.status["publish"]["error"]); self.assertIsNotNone(server.status["publish"]["ok_at"])
        self.assertFalse(r.step(10 ** 6)); self.assertEqual(r.fails, 0)

    def test_backoff_caps_at_five_minutes(self):
        r = server.PublishRetry(); r.fails = 10
        r.step(0); self.assertEqual(r.next_at, 300)


class RefreshResultTest(unittest.TestCase):
    """refresh 가 실패한 레포를 돌려주는지(감시 재시도의 근거). state 는 임시 파일에만 쓴다."""
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json"); os.close(fd)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"collected_at": "t0", "errors": [], "repos": [{"name": "a"}, {"name": "b"}]}, f)
        self.orig = (server.STATE, collector.Collector.collect, collector.load_cfg, server.OPT.get("publish"))
        server.STATE, server.OPT["publish"] = self.path, None
        collector.load_cfg = lambda path=None: {"org": "o", "repos": [{"name": "a"}, {"name": "b"}]}

    def tearDown(self):
        server.STATE, collector.Collector.collect, collector.load_cfg, server.OPT["publish"] = self.orig
        os.remove(self.path)

    def test_repo_with_errors_is_failed(self):
        collector.Collector.collect = lambda s, only=None: {"collected_at": "t1", "duration_s": 1, "errors": [], "repos": [
            {"name": "a", "errors": ["Azure 수집 실패 — x"]}]}
        self.assertEqual(server.refresh(["a"]), ["a"])
        collector.Collector.collect = lambda s, only=None: {"collected_at": "t2", "duration_s": 1, "errors": [], "repos": [{"name": "a", "errors": []}]}
        self.assertEqual(server.refresh(["a"]), [])

    def test_exception_fails_all_targets(self):
        def boom(s, only=None):
            raise RuntimeError("gh 끊김")
        collector.Collector.collect = boom
        self.assertEqual(server.refresh(["b"]), ["b"])
        self.assertEqual(server.refresh(), ["a", "b"])
        self.assertIn("gh 끊김", server.status["last_error"])

    def test_busy_returns_none(self):
        server.status["running"] = True
        try:
            self.assertIsNone(server.refresh(["a"]))
        finally:
            server.status["running"] = False


if __name__ == "__main__":
    unittest.main()
