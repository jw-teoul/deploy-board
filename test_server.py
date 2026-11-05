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


if __name__ == "__main__":
    unittest.main()
