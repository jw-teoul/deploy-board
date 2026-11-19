"""swap 권한 판정·올리기 대상 판별 테스트(az·ssh 호출 없음). python -m unittest test_swap"""
import os
import tempfile
import unittest

import collector
import json
import publish
import swap

A = swap.SWAP_ACTION
# 11-04 실제 조회와 같은 모양(PilldocSiteNew-Prod·agentapi2 = 가능, AdminSiteLinux-Prod·AdpsSiteLinux-Prod = 불가)
OPEN = [{"actions": ["Microsoft.Web/sites/*"], "notActions": []},
        {"actions": ["*/read"], "notActions": []}]
BLOCKED = [{"actions": ["Microsoft.Web/sites/*"], "notActions": ["Microsoft.Web/sites/slotsswap/Action", "Microsoft.Web/sites/delete"]},
           {"actions": ["*/read"], "notActions": []}]


class SwapAllowed(unittest.TestCase):
    def test_open(self):
        self.assertTrue(swap.swap_allowed(OPEN)[0])

    def test_blocked_by_not_actions(self):
        ok, why = swap.swap_allowed(BLOCKED)
        self.assertFalse(ok)
        self.assertIn("slotsswap", why)

    def test_other_role_allows(self):  # 한 역할이 막아도 다른 역할이 허용하면 허용(RBAC 는 합집합)
        self.assertTrue(swap.swap_allowed(BLOCKED + [{"actions": ["*"], "notActions": []}])[0])

    def test_read_only_and_empty(self):
        self.assertFalse(swap.swap_allowed([{"actions": ["*/read"], "notActions": []}])[0])
        self.assertFalse(swap.swap_allowed([])[0])
        self.assertFalse(swap.swap_allowed(None)[0])

    def test_shared_cases(self):  # 웹 로그인(weblogin.js, test_weblogin.js)과 같은 사례로 같은 답
        import json
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "perm_cases.json"), encoding="utf-8") as f:
            cases = json.load(f)
        for c in cases["allowed"]:
            ok, why = swap.swap_allowed(c["perms"])
            self.assertEqual(ok, c["ok"], c["name"])
            if c.get("why"):
                self.assertIn(c["why"], why, c["name"])
        for pattern, ok in cases["match"]:
            self.assertEqual(swap.action_match(pattern, A), ok, pattern)

    def test_case_and_wildcards(self):
        self.assertTrue(swap.action_match("microsoft.web/SITES/*", A))
        self.assertTrue(swap.action_match("*", A))
        self.assertFalse(swap.action_match("Microsoft.Web/sites/slots/*", A))
        self.assertFalse(swap.action_match("Microsoft.Web/sites/slotsswap", A))


class Publish(unittest.TestCase):
    def test_is_remote(self):
        self.assertTrue(publish.is_remote("datacrawler:deploy-board/data/state.json"))
        self.assertFalse(publish.is_remote(r"C:\x\state.json"))
        self.assertFalse(publish.is_remote("data/out.json"))
        self.assertFalse(publish.is_remote("./a:b/out.json"))

    def test_local_copy(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "s.json"), os.path.join(d, "sub", "t.json")
            with open(src, "w") as f:
                f.write('{"a":1}')
            self.assertIsNone(publish.publish(dst, src))
            with open(dst) as f:
                self.assertEqual(f.read(), '{"a":1}')
            self.assertFalse(os.path.exists(dst + ".tmp"))
            self.assertIn("없음", publish.publish(dst, os.path.join(d, "none.json")))




class FakeAz:
    """az 대신 답한다 — login: 로컬 계정 정보(az account show), token: 토큰이 살아 있나, perms: 앱 → permissions value 또는 예외."""

    def __init__(self, login=True, token=True, perms=None):
        self.login, self.token, self.perms, self.calls = login, token, perms or {}, []

    def __call__(self, cmd, timeout=None, **k):
        self.calls.append(cmd)
        if cmd[:3] == ["az", "account", "show"]:
            if not self.login:
                raise RuntimeError("Please run 'az login' to setup account.")
            return '{"user": "me@x", "sub": "구독"}' if "{user" in " ".join(cmd) else "SUB"
        if cmd[:3] == ["az", "account", "get-access-token"]:
            if not self.token:
                raise RuntimeError("AADSTS700082: The refresh token has expired")
            return "2026-11-19 15:00:00.000000"
        if cmd[:2] == ["gh", "api"]:
            return "me"
        if cmd[:2] == ["az", "rest"]:
            if not self.token:
                raise RuntimeError("AADSTS700082: The refresh token has expired")
            v = next((v for app, v in self.perms.items() if f"/sites/{app}/" in cmd[-1]), None)
            if callable(v):   # 시험이 조회 도중을 붙잡을 때
                v = v()
            if isinstance(v, Exception):
                raise v
            return json.dumps({"value": v if v is not None else OPEN})
        raise RuntimeError(f"예상 밖 명령 {cmd}")


class Live(unittest.TestCase):
    """로그인 표시·권한 캐시가 실제 az 상태와 어긋나지 않게(11-19 사용자 보고 「로그인 상태와 swap 버튼이 애매」)."""

    def setUp(self):
        self.cfg = {"org": "o", "resource_group": "rg", "repos": [{"name": "a", "app": "A"}, {"name": "b", "app": "B"}]}
        self.az = FakeAz()
        self.orig = (collector.sh, swap.collector.sh)
        swap.collector.sh = self.az
        self.addCleanup(self._restore)
        self.r = swap.SwapRunner(lambda: self.cfg, os.path.join(tempfile.mkdtemp(), "log.jsonl"), on_done=lambda n: None)
        self.r.fake = None

    def _restore(self):
        collector.sh = self.orig[0]

    def test_older_check_cannot_overwrite_newer_live_check(self):
        """주기 확인이 「허용」을 받아 오는 사이 계정이 바뀌어 live_check 가 실패를 적으면, 늦게 끝난 주기 확인이 허용으로 덮지 않는다."""
        import threading
        started, release = threading.Event(), threading.Event()
        self.az.perms["A"] = lambda: (started.set(), release.wait(5), OPEN)[2]
        t = threading.Thread(target=self.r.check_perms)
        t.start()
        self.assertTrue(started.wait(5))
        self.az.perms["A"] = RuntimeError("AuthorizationFailed")
        ok, _ = self.r.live_check("a")
        self.assertFalse(ok)
        release.set()
        t.join(5)
        self.assertFalse(self.r.status["perms"]["a"]["ok"])
        self.assertTrue(self.r.status["perms"]["a"]["error"])
        self.assertEqual(self.r.status["perms_failed"], 1)   # 집계도 캐시와 같다

    def test_older_failure_cannot_overwrite_newer_allow(self):
        import threading
        started, release = threading.Event(), threading.Event()
        self.az.perms["A"] = lambda: (started.set(), release.wait(5), RuntimeError("일시 오류"))[2]
        t = threading.Thread(target=self.r.check_perms)
        t.start()
        self.assertTrue(started.wait(5))
        self.az.perms["A"] = OPEN
        self.assertTrue(self.r.live_check("a")[0])
        release.set()
        t.join(5)
        self.assertTrue(self.r.status["perms"]["a"]["ok"])
        self.assertEqual(self.r.status["perms_failed"], 0)

    def test_live_check_failure_updates_failed_count(self):
        self.r.check_perms()
        self.assertEqual(self.r.status["perms_failed"], 0)
        self.az.token = False
        self.r.live_check("a")
        self.assertEqual(self.r.status["perms_failed"], 1)

    def test_malformed_response_is_error_not_crash(self):
        self.az.perms["A"] = [1, "x"]
        ok, why = self.r.live_check("a")
        self.assertFalse(ok)
        self.assertTrue(self.r.status["perms"]["a"]["error"])
        self.r.check_perms()
        self.assertTrue(self.r.status["perms"]["a"]["error"])
        self.assertTrue(self.r.status["perms"]["b"]["ok"])

    def test_wrong_shape_is_error_not_allow(self):   # 예외는 안 나지만 모양이 틀린 응답 — 허용으로 읽지 않는다(Codex 3차)
        for bad in ([{"actions": ["*"], "notActions": None}], [{"actions": ["*"]}], [{"actions": "*", "notActions": []}],
                    [{"actions": ["*"], "notActions": [None]}], {"x": 1}):
            self.az.perms["A"] = bad
            ok, _ = self.r.live_check("a")
            self.assertFalse(ok, bad)
            self.assertTrue(self.r.status["perms"]["a"]["error"], bad)

    def test_check_crash_keeps_newer_live_result(self):
        """주기 확인이 도중에 터져 전체 실패로 바꿀 때도 그 확인보다 늦게 시작한 live_check 결과는 덮지 않는다."""
        import threading
        started, release = threading.Event(), threading.Event()
        self.az.perms["A"] = lambda: (started.set(), release.wait(5), OPEN)[2]
        orig = self.r.check_perms
        def crash(seq=None):
            orig(seq)
            raise RuntimeError("뒷정리 중 오류")
        self.r.check_perms = crash
        self.assertTrue(self.r.begin_check())
        t = threading.Thread(target=self.r.run_check)
        t.start()
        self.assertTrue(started.wait(5))
        self.assertTrue(self.r.live_check("a")[0])
        release.set()
        t.join(5)
        self.assertTrue(self.r.status["perms"]["a"]["ok"])
        self.assertTrue(self.r.status["perms"]["b"]["error"])

    def test_round_counts_each_check(self):   # 같은 초에 두 번 끝나도 화면이 「새로 확인함」을 안다
        self.r.check_perms()
        self.r.check_perms()
        self.assertEqual(self.r.status["perms_round"], 2)

    def test_header_says_expired_when_token_dead(self):
        self.az.token = False
        a = swap.whoami(self.az)["az"]
        self.assertFalse(a["ok"])
        self.assertIn("만료", a["reason"])

    def test_header_ok_when_token_alive(self):
        a = swap.whoami(self.az)["az"]
        self.assertTrue(a["ok"])
        self.assertEqual(a["user"], "me@x")

    def test_lookup_failure_is_error_not_denied(self):
        self.az.perms = {"A": RuntimeError("timeout")}
        self.r.recheck()
        self.assertEqual(self.r.status["perms"]["a"]["ok"], False)
        self.assertTrue(self.r.status["perms"]["a"].get("error"))
        self.assertTrue(self.r.status["perms"]["b"]["ok"])
        self.assertFalse(self.r.status["perms"]["b"].get("error"))
        self.assertEqual(self.r.status["perms_failed"], 1)

    def test_denied_is_not_error(self):
        self.az.perms = {"A": BLOCKED}
        self.r.recheck()
        self.assertFalse(self.r.status["perms"]["a"]["ok"])
        self.assertFalse(self.r.status["perms"]["a"].get("error"))

    def test_check_crash_drops_old_allow(self):
        self.r.recheck()
        self.assertTrue(self.r.status["perms"]["a"]["ok"])
        self.r.cfg_loader = lambda: (_ for _ in ()).throw(ValueError("repos.json 깨짐"))
        self.r.recheck()
        self.assertFalse(self.r.status["perms"]["a"]["ok"])
        self.assertTrue(self.r.status["perms"]["a"]["error"])

    def test_logged_out_drops_old_allow(self):
        self.r.recheck()
        self.az.login = False
        self.r.recheck()
        self.assertFalse(self.r.status["perms"]["a"]["ok"])
        self.assertTrue(self.r.status["perms"]["a"]["error"])

    def test_live_check_catches_expired_token(self):
        self.r.recheck()
        self.assertTrue(self.r.can_start("a", "swap")[0])
        self.az.token = False
        ok, why = self.r.live_check("a")
        self.assertFalse(ok)
        self.assertIn("AADSTS700082", why)
        self.assertFalse(self.r.can_start("a", "swap")[0])   # 캐시도 고쳐 버튼이 회색으로 바뀐다

    def test_live_check_catches_revoked_permission(self):
        self.r.recheck()
        self.az.perms = {"A": BLOCKED}
        ok, why = self.r.live_check("a")
        self.assertFalse(ok)
        self.assertIn("slotsswap", why)

    def test_live_check_ok(self):
        self.assertEqual(self.r.live_check("a"), (True, "'Microsoft.Web/sites/*' 로 허용"))
        self.assertTrue(self.r.status["perms"]["a"]["ok"])

    def test_live_check_skipped_for_fake_command(self):
        self.r.fake = "python -c pass"
        self.az.token = False
        self.assertTrue(self.r.live_check("a")[0])
        self.assertEqual(self.az.calls, [])

    def test_loop_checks_accounts_with_perms(self):
        seen = []
        self.r.recheck(before=lambda: seen.append("accounts"))
        self.assertEqual(seen, ["accounts"])


if __name__ == "__main__":
    unittest.main()
