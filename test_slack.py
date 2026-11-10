"""Slack 봇 전송 테스트 — 실제 Slack·az 호출 없음. 127.0.0.1 임시 포트의 가짜 Slack API 와 가짜 swap 명령만 쓴다.
python -m unittest test_slack"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import server
import slack
import swap

TOKEN = "xoxb-test-secret-123"
ORIG_REPO_STATE = server.repo_state
CHANNEL = "C0TESTCHAN"


class FakeSlack:
    """chat.postMessage 를 흉내 낸다. fail_next 개수만큼 실패(ok:false)로 답한다."""
    def __init__(self):
        self.calls, self.fail_main, self.fail_thread, self.n, self.no_ts = [], False, False, 0, False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                fail = fake.fail_thread if body.get("thread_ts") else fake.fail_main
                fake.n += 1
                out = {"ok": False, "error": "channel_not_found"} if fail else {"ok": True, "ts": f"1700000000.{fake.n:06d}"}
                if fake.no_ts and not fail:
                    out.pop("ts")
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.api = "http://127.0.0.1:%d/api" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Config(unittest.TestCase):
    def test_off_without_values(self):
        for env in ({}, {"DEPLOY_BOARD_SLACK_TOKEN": TOKEN}, {"DEPLOY_BOARD_SLACK_CHANNEL": CHANNEL}):
            cfg, why = slack.config(env)
            self.assertIsNone(cfg)
            self.assertIn("환경 변수", why)

    def test_bad_values(self):
        self.assertIsNone(slack.config({"DEPLOY_BOARD_SLACK_TOKEN": "xoxp-user", "DEPLOY_BOARD_SLACK_CHANNEL": CHANNEL})[0])
        self.assertIsNone(slack.config({"DEPLOY_BOARD_SLACK_TOKEN": TOKEN, "DEPLOY_BOARD_SLACK_CHANNEL": "#배포"})[0])

    def test_on_and_public_has_no_token(self):
        cfg, _ = slack.config({"DEPLOY_BOARD_SLACK_TOKEN": TOKEN, "DEPLOY_BOARD_SLACK_CHANNEL": CHANNEL})
        self.assertEqual(cfg["api"], slack.DEFAULT_API)
        pub = slack.public(cfg)
        self.assertEqual(pub, {"enabled": True, "channel": CHANNEL})
        self.assertNotIn(TOKEN, json.dumps(pub))

    def test_api_must_be_https_except_loopback(self):
        base = {"DEPLOY_BOARD_SLACK_TOKEN": TOKEN, "DEPLOY_BOARD_SLACK_CHANNEL": CHANNEL}
        self.assertIsNone(slack.config(dict(base, DEPLOY_BOARD_SLACK_API="http://slack.example.com/api"))[0])
        self.assertIsNone(slack.config(dict(base, DEPLOY_BOARD_SLACK_API="http://127.0.0.1.evil.com/api"))[0])
        self.assertIsNotNone(slack.config(dict(base, DEPLOY_BOARD_SLACK_API="https://slack.example.com/api"))[0])
        self.assertIsNotNone(slack.config(dict(base, DEPLOY_BOARD_SLACK_API="http://127.0.0.1:8798/api"))[0])

    def test_links_from_repos_json(self):
        base = {"DEPLOY_BOARD_SLACK_TOKEN": TOKEN, "DEPLOY_BOARD_SLACK_CHANNEL": CHANNEL}
        cfg, _ = slack.config(base, links={"jira_base": "https://edb.atlassian.net/browse/", "issue_key_pattern": r"\bPILLDOC-\d+\b"})
        self.assertEqual(cfg["jira_base"], "https://edb.atlassian.net/browse/")
        self.assertEqual(cfg["key_re"].pattern, r"\bPILLDOC-\d+\b")
        self.assertNotIn("jira_base", slack.config(base)[0])   # 설정이 없으면 링크 없이


class EnvFile(unittest.TestCase):
    def test_reads_board_keys_without_overriding(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w", encoding="utf-8") as f:
                f.write("# 주석\n\nDEPLOY_BOARD_SLACK_TOKEN=" + TOKEN + "\n"
                        'DEPLOY_BOARD_SLACK_CHANNEL="C0FROMFILE"\n'
                        "DEPLOY_BOARD_SLACK_APP_TOKEN = xapp-1-abc \n"
                        "PATH=/evil\nDEPLOY_BOARD_SLACK_API=\n")
            env = {"DEPLOY_BOARD_SLACK_CHANNEL": "C0ALREADY", "PATH": "/usr/bin"}
            keys = slack.load_env_file(p, env)
        self.assertEqual(env["DEPLOY_BOARD_SLACK_TOKEN"], TOKEN)
        self.assertEqual(env["DEPLOY_BOARD_SLACK_CHANNEL"], "C0ALREADY")   # 이미 있는 값은 그대로
        self.assertEqual(env["DEPLOY_BOARD_SLACK_APP_TOKEN"], "xapp-1-abc")
        self.assertEqual(env["PATH"], "/usr/bin")   # 보드 키가 아니면 읽지 않는다
        self.assertNotIn("DEPLOY_BOARD_SLACK_API", env)   # 빈 값은 넣지 않는다
        self.assertEqual(sorted(keys), ["DEPLOY_BOARD_SLACK_APP_TOKEN", "DEPLOY_BOARD_SLACK_TOKEN"])

    def test_missing_file_is_fine(self):
        self.assertEqual(slack.load_env_file(os.path.join(tempfile.gettempdir(), "없는-파일.env"), {}), [])


class Post(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSlack()
        self.cfg = {"token": TOKEN, "channel": CHANNEL, "api": self.fake.api}

    def tearDown(self):
        self.fake.close()

    def test_post_and_thread(self):
        ok, ts, err = slack.post(self.cfg, "예고")
        self.assertTrue(ok, err)
        ok2, _, _ = slack.post(self.cfg, "결과", thread_ts=ts)
        self.assertTrue(ok2)
        c0, c1 = self.fake.calls
        self.assertEqual(c0["path"], "/api/chat.postMessage")
        self.assertEqual(c0["auth"], "Bearer " + TOKEN)
        self.assertEqual(c0["body"]["channel"], CHANNEL)
        self.assertNotIn("thread_ts", c0["body"])
        self.assertEqual(c1["body"]["thread_ts"], ts)

    def test_errors_never_raise_or_leak_token(self):
        self.fake.fail_main = True
        ok, ts, err = slack.post(self.cfg, "예고")
        self.assertFalse(ok)
        self.assertIn("channel_not_found", err)
        bad = dict(self.cfg, api="http://127.0.0.1:1/" + TOKEN)   # 접속 실패 오류 문구에 토큰이 섞여도 가린다
        ok, _, err = slack.post(bad, "예고", timeout=2)
        self.assertFalse(ok)
        self.assertNotIn(TOKEN, err)

    def test_empty_and_too_long(self):
        self.assertFalse(slack.post(self.cfg, "  ")[0])
        self.assertFalse(slack.post(self.cfg, "가" * (slack.MAX_TEXT + 1))[0])
        self.assertEqual(self.fake.calls, [])

    def test_control_chars_escaped(self):
        ok, _, err = slack.post(self.cfg, "*제목* <!channel> <https://evil|링크> a&b")
        self.assertTrue(ok, err)
        self.assertEqual(self.fake.calls[0]["body"]["text"], "*제목* &lt;!channel&gt; &lt;https://evil|링크&gt; a&amp;b")

    def test_issue_keys_become_jira_links(self):
        import re
        cfg = dict(self.cfg, jira_base="https://edb.atlassian.net/browse/", key_re=re.compile(r"\b(?:PILLDOC|PI)-\d+\b"))
        ok, _, err = slack.post(cfg, "• 수정 PILLDOC-12 UTF-8 <봉투> & PI-3")
        self.assertTrue(ok, err)
        self.assertEqual(self.fake.calls[0]["body"]["text"],
                         "• 수정 <https://edb.atlassian.net/browse/PILLDOC-12|PILLDOC-12> UTF-8 &lt;봉투&gt; &amp; "
                         "<https://edb.atlassian.net/browse/PI-3|PI-3>")

    def test_no_links_without_config(self):
        slack.post(self.cfg, "PILLDOC-12")
        self.assertEqual(self.fake.calls[0]["body"]["text"], "PILLDOC-12")

    def test_ok_without_ts_is_failure(self):
        self.fake.no_ts = True
        ok, ts, err = slack.post(self.cfg, "예고")
        self.assertFalse(ok)
        self.assertIn("ts", err)


class SwapBase(unittest.TestCase):
    """server.slack_preview → SwapRunner(가짜 명령) → server.slack_result 흐름."""

    def setUp(self):
        self.fake = FakeSlack()
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "swap-log.jsonl")
        self.old_env = os.environ.get("DEPLOY_BOARD_SWAP_CMD")
        self.set_cmd(0)
        self.saved = dict(server.OPT)
        server.OPT["slack"] = {"token": TOKEN, "channel": CHANNEL, "api": self.fake.api}
        cfg = {"resource_group": "rg", "repos": [{"name": "pilldocsite", "app": "App-Prod", "label": "필독 사이트"}]}
        self.runner = swap.SwapRunner(lambda: cfg, self.log, on_done=lambda _: None, notify=server.slack_result)
        self.runner.status["perms"]["pilldocsite"] = {"ok": True, "reason": "시험"}
        server.OPT["swap"] = self.runner

    def tearDown(self):
        server.OPT.clear()
        server.OPT.update(self.saved)
        if self.old_env is None:
            os.environ.pop("DEPLOY_BOARD_SWAP_CMD", None)
        else:
            os.environ["DEPLOY_BOARD_SWAP_CMD"] = self.old_env
        self.fake.close()
        self.tmp.cleanup()

    def set_cmd(self, rc):
        os.environ["DEPLOY_BOARD_SWAP_CMD"] = f'"{sys.executable}" -c "import sys; sys.exit({rc})"'

    def snap(self):
        return {"label": "필독 사이트", "commits": 1, "known": True, "nokey": [],
                "items": [{"key": "PILLDOC-1", "title": "약국 검색", "kind": "추가"}]}

    def run_swap(self, action="swap", req=None, rc=0):
        self.set_cmd(rc)
        self.runner.fake = os.environ["DEPLOY_BOARD_SWAP_CMD"]
        info = {"snapshot": self.snap() if action == "swap" else {"label": "필독 사이트"}}
        err = server.slack_preview("pilldocsite", action, info, req)
        if err:
            return err, None
        ok, why = self.runner.start("pilldocsite", action, info)
        self.assertTrue(ok, why)
        for _ in range(200):
            if not self.runner.status["job"]:
                break
            time.sleep(0.05)
        return None, self.runner.status["last"]

    def log_lines(self):
        with open(self.log, encoding="utf-8") as f:
            return f.read()

class SwapFlow(SwapBase):
    def test_preview_then_result_in_thread_then_undo_same_thread(self):
        err, done = self.run_swap(req={"text": "*필독 사이트 운영 반영 예정*"})
        self.assertIsNone(err)
        pre, res = self.fake.calls
        self.assertEqual(pre["body"]["text"], "*필독 사이트 운영 반영 예정*")
        self.assertEqual(res["body"]["thread_ts"], done["info"]["slack"]["ts"])
        self.assertIn("운영 swap 완료", res["body"]["text"])
        self.assertTrue(done["slack"]["result_ok"])
        _, undone = self.run_swap(action="undo")
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(self.fake.calls[2]["body"]["thread_ts"], pre_ts := done["info"]["slack"]["ts"])
        self.assertIn("되돌림", self.fake.calls[2]["body"]["text"])
        self.assertEqual(undone["slack"]["ts"], pre_ts)

    def test_failed_swap_result_in_thread(self):
        _, done = self.run_swap(req={"text": "예고"}, rc=1)
        self.assertFalse(done["ok"])
        self.assertIn("실패", self.fake.calls[1]["body"]["text"])
        self.assertNotIn("pilldocsite", self.runner.status["undo"])   # 실패한 swap 은 되돌리기 대상이 아님

    def test_failed_undo_result_in_thread(self):
        _, done = self.run_swap(req={"text": "예고"})
        _, undone = self.run_swap(action="undo", rc=1)
        self.assertFalse(undone["ok"])
        text = self.fake.calls[2]["body"]["text"]
        self.assertEqual(self.fake.calls[2]["body"]["thread_ts"], done["info"]["slack"]["ts"])
        self.assertIn("되돌리기 실패", text)
        self.assertIn("새 버전으로 운영 중", text)
        self.assertNotIn("운영 swap 실패", text)

    def test_preview_failure_blocks_swap(self):
        self.fake.fail_main = True
        err, done = self.run_swap(req={"text": "예고"})
        self.assertIn("swap 하지 않음", err)
        self.assertIsNone(done)
        self.assertIsNone(self.runner.status["last"])

    def test_no_preview_when_swap_cannot_start(self):
        self.runner.status["perms"]["pilldocsite"] = {"ok": False, "reason": "권한 없음"}
        err, _ = self.run_swap(req={"text": "예고"})
        self.assertIn("권한", err)
        self.assertEqual(self.fake.calls, [])

    def test_thread_failure_keeps_swap_result(self):
        self.fake.fail_thread = True
        _, done = self.run_swap(req={"text": "예고"})
        self.assertTrue(done["ok"])
        self.assertFalse(done["slack"]["result_ok"])
        self.assertIn("channel_not_found", done["slack"]["result_error"])
        self.assertEqual(len(self.fake.calls), 2)   # 다시 보내지 않는다
        self.assertIn("pilldocsite", self.runner.status["undo"])

    def test_unchecked_sends_nothing(self):
        _, done = self.run_swap(req=None)
        self.assertTrue(done["ok"])
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn("slack", done)

    def test_slack_off_rejects_checked(self):
        server.OPT["slack"] = None
        err, _ = self.run_swap(req={"text": "예고"})
        self.assertIn("꺼져", err)

    def test_empty_or_long_text_rejected(self):
        self.assertIn("비어", self.run_swap(req={"text": " "})[0])
        self.assertIn("넘음", self.run_swap(req={"text": "가" * (slack.MAX_TEXT + 1)})[0])
        self.assertEqual(self.fake.calls, [])

    def test_token_not_in_log_or_status(self):
        self.run_swap(req={"text": "예고"})
        self.assertNotIn(TOKEN, self.log_lines())
        self.assertNotIn(TOKEN, json.dumps(self.runner.status, ensure_ascii=False))
        self.assertNotIn(TOKEN, json.dumps(server.status, ensure_ascii=False, default=str))


class Endpoint(SwapBase):
    """/api/swap 요청 그대로(loopback·토큰 검사 포함)."""

    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        server.OPT["port"] = self.httpd.server_address[1]
        server.OPT["token"] = "board-token"
        server.repo_state = lambda name: {"name": name, "label": "필독 사이트", "url": "https://www.pilldoc.co.kr",
                                          "staging_only": {"commits": []}, "issues": []}
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.repo_state = ORIG_REPO_STATE
        super().tearDown()

    def call(self, body):
        import urllib.error
        import urllib.request
        url = "http://127.0.0.1:%d/api/swap" % server.OPT["port"]
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Board-Token": "board-token"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait(self):
        for _ in range(200):
            if not self.runner.status["job"]:
                return
            time.sleep(0.05)

    def test_swap_with_slack_then_undo(self):
        self.runner.fake = os.environ["DEPLOY_BOARD_SWAP_CMD"]
        code, j = self.call({"repo": "pilldocsite", "action": "swap", "slack": {"text": "예고 문구"}})
        self.assertEqual(code, 202, j)
        self.wait()
        self.assertEqual([c["body"]["text"] for c in self.fake.calls][0], "예고 문구")
        self.assertEqual(len(self.fake.calls), 2)
        self.assertNotIn(TOKEN, json.dumps(j, ensure_ascii=False))
        code, j = self.call({"repo": "pilldocsite", "action": "undo"})
        self.assertEqual(code, 202, j)
        self.wait()
        self.assertEqual(self.fake.calls[2]["body"]["thread_ts"], self.fake.calls[0]["body"].get("thread_ts") or self.runner.status["last"]["slack"]["ts"])
        self.assertIn("필독 사이트 swap 되돌림", self.fake.calls[2]["body"]["text"])
        self.assertIn("확인: https://www.pilldoc.co.kr", self.fake.calls[2]["body"]["text"])   # 되돌리기 기록에도 사이트 주소

    def test_preview_failure_returns_409_and_no_swap(self):
        self.fake.fail_main = True
        code, j = self.call({"repo": "pilldocsite", "action": "swap", "slack": {"text": "예고"}})
        self.assertEqual(code, 409)
        self.assertIn("swap 하지 않음", j["reason"])
        self.assertIsNone(self.runner.status["job"])
        self.assertIsNone(self.runner.status["last"])

    def post(self, body, token="board-token"):
        import urllib.error
        import urllib.request
        url = "http://127.0.0.1:%d/api/slack" % server.OPT["port"]
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Board-Token": token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_send_result_text(self):   # 「알림 문구」 창의 「Slack 으로 보내기」 — 고친 문구를 채널에 새 글로 보낸다
        code, j = self.post({"repo": "pilldocsite", "text": "*필독 사이트 운영 swap 완료*"})
        self.assertEqual(code, 200, j)
        self.assertTrue(j["sent"])
        self.assertEqual(self.fake.calls[0]["body"]["text"], "*필독 사이트 운영 swap 완료*")
        self.assertNotIn("thread_ts", self.fake.calls[0]["body"])
        self.assertNotIn(TOKEN, json.dumps(j, ensure_ascii=False))

    def test_send_rejects(self):
        self.assertEqual(self.post({"repo": "pilldocsite", "text": "x"}, token="bad")[0], 403)
        self.assertEqual(self.post({"repo": "pilldocsite", "text": " "})[0], 400)
        self.assertEqual(self.post({"repo": "pilldocsite", "text": "가" * (slack.MAX_TEXT + 1)})[0], 400)
        server.OPT["slack"] = None
        self.assertEqual(self.post({"repo": "pilldocsite", "text": "x"})[0], 409)
        self.assertEqual(self.fake.calls, [])

    def test_send_failure(self):
        self.fake.fail_main = True
        code, j = self.post({"repo": "pilldocsite", "text": "x"})
        self.assertEqual(code, 502)
        self.assertFalse(j["sent"])

    def test_start_fails_after_preview(self):
        self.runner.start = lambda repo, action, info: (False, "이미 진행 중")   # can_start 뒤 경합
        code, j = self.call({"repo": "pilldocsite", "action": "swap", "slack": {"text": "예고"}})
        self.assertEqual(code, 409)
        self.assertTrue(j["slack_sent"])
        self.assertIn("예고는 이미 보냄", j["reason"])
        self.assertEqual(len(self.fake.calls), 2)
        self.assertIn("swap 시작 못 함", self.fake.calls[1]["body"]["text"])
        self.assertTrue(self.fake.calls[1]["body"]["thread_ts"])


if __name__ == "__main__":
    unittest.main()
