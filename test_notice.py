"""배포 알림 문구·로그인 확인 테스트(az·gh·Slack 호출 없음). python -m unittest test_notice"""
import json
import os
import tempfile
import unittest

import notice
import swap


def commit(sha, msg, keys=()):
    return {"sha": sha, "author": "a", "date": "2026-11-05T00:00:00Z", "message": msg, "url": "", "keys": list(keys)}


def repo(commits, issues=(), known=True):
    return {"name": "pilldocsite", "label": "필독 사이트", "app": "PilldocSiteNew-Prod", "url": "https://www.pilldoc.co.kr",
            "prod": {"sha": "p1"}, "staging": {"sha": "s1"},
            "staging_only": {"commits": commits} if known else None,
            "issues": list(issues)}


class Kind(unittest.TestCase):
    def test_conventional(self):
        self.assertEqual(notice.kind_of("feat: 약국 검색"), "추가")
        self.assertEqual(notice.kind_of("fix(api): 500 오류"), "수정")
        self.assertEqual(notice.kind_of("[PILLDOC-1] chore: 정리"), "변경")

    def test_korean_words(self):
        self.assertEqual(notice.kind_of("봉투 문구 누락 수정"), "수정")
        self.assertEqual(notice.kind_of("마일리지 내역 화면 추가"), "추가")

    def test_clean_title(self):
        self.assertEqual(notice.clean_title("[PILLDOC-12] 봉투 문구", "PILLDOC-12"), "봉투 문구")
        self.assertEqual(notice.clean_title("PILLDOC-12: 봉투 문구\n본문", "PILLDOC-12"), "봉투 문구")
        self.assertEqual(notice.clean_title(""), "(제목 없음)")
        # 키 뒤에 이어 붙인 다른 키 줄임 표기(·3091·3088)도 함께 뗀다
        self.assertEqual(notice.clean_title("PILLDOC-3087·3091·3088 운영 반영 — x", "PILLDOC-3087"), "운영 반영 — x")
        self.assertEqual(notice.clean_title("[PILLDOC-1, PILLDOC-2] 봉투", "PILLDOC-1"), "봉투")
        self.assertEqual(notice.clean_title("PILLDOC-1 2개 수정", "PILLDOC-1"), "2개 수정")


class Snapshot(unittest.TestCase):
    def test_groups_by_key_and_uses_issue_title(self):
        r = repo([commit("a", "PILLDOC-1 wip", ["PILLDOC-1"]), commit("b", "PILLDOC-1 more", ["PILLDOC-1"]),
                  commit("c", "fix: 오타", [])],
                 issues=[{"key": "PILLDOC-1", "title": "feat: [PILLDOC-1] 약국 검색"}])
        s = notice.snapshot(r)
        self.assertEqual(s["commits"], 3)
        self.assertEqual(s["items"], [{"key": "PILLDOC-1", "title": "약국 검색", "kind": "추가"}])
        self.assertEqual(s["nokey"][0]["title"], "오타")
        self.assertEqual(s["staging_sha"], "s1")
        self.assertEqual(s["url"], "https://www.pilldoc.co.kr")
        json.dumps(s)  # swap-log 에 그대로 쓸 수 있어야 한다

    def test_unknown_prod(self):
        s = notice.snapshot(repo([], known=False))
        self.assertFalse(s["known"])
        self.assertIsNone(notice.preview_text(s)[0])


class Preview(unittest.TestCase):
    def test_nothing_to_ship(self):
        text, why = notice.preview_text(notice.snapshot(repo([])))
        self.assertIsNone(text)
        self.assertIn("없음", why)

    def test_format(self):
        r = repo([commit("a", "x", ["PILLDOC-1"]), commit("b", "fix: 오타", [])],
                 issues=[{"key": "PILLDOC-1", "title": "약국 검색 추가"}])
        text, _ = notice.preview_text(notice.snapshot(r))
        lines = text.split("\n")
        self.assertEqual(lines[0], "*필독 사이트 운영 swap 시작 — 이슈 1건, 기타 1건*")
        self.assertEqual(lines[1:3], ["• 추가 PILLDOC-1 약국 검색 추가", "• 수정 오타"])
        self.assertEqual(lines[-1], "_https://www.pilldoc.co.kr · PilldocSiteNew-Prod staging → production · 결과는 이 글 스레드에_")
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)   # 키를 코드로 감싸면 봇 전송 때 Jira 링크가 되지 않는다

    def test_footer_without_url(self):
        r = dict(repo([commit("b", "fix: 오타", [])]), url=None)
        text, _ = notice.preview_text(notice.snapshot(r))
        self.assertEqual(text.split("\n")[0], "*필독 사이트 운영 swap 시작 — 기타 1건*")
        self.assertEqual(text.split("\n")[-1], "_PilldocSiteNew-Prod staging → production · 결과는 이 글 스레드에_")

    def test_limits(self):  # 이슈가 많고 제목이 길어도 300자·8줄 안, 넘친 것은 「외 N건」
        cs = [commit(str(i), "x", [f"PILLDOC-{i}"]) for i in range(12)]
        iss = [{"key": f"PILLDOC-{i}", "title": "아주 긴 제목 " * 20} for i in range(12)]
        text, _ = notice.preview_text(notice.snapshot(repo(cs, iss)))
        self.assertLessEqual(len(text), notice.MAX_CHARS)
        self.assertLessEqual(len(text.split("\n")), notice.MAX_LINES)
        self.assertIn("• 외 8건", text.split("\n"))
        self.assertTrue(text.split("\n")[-1].startswith("_https://www.pilldoc.co.kr"))   # 꼬리말은 줄이지 않는다
        self.assertIn("…", text)


class Result(unittest.TestCase):
    def rec(self, **kw):
        r = repo([commit("a", "x", ["PILLDOC-1"]), commit("b", "fix: 오타", []), commit("c", "y", ["PILLDOC-2"])],
                 issues=[{"key": "PILLDOC-1", "title": "약국 검색 추가"}, {"key": "PILLDOC-2", "title": "정리"}])
        base = {"repo": "pilldocsite", "action": "swap", "ok": True, "finished_at": "2026-11-05T05:30:00+00:00",
                "info": {"snapshot": notice.snapshot(r)}}
        base.update(kw)
        return base

    def test_success_sorted_by_kind(self):
        text, _ = notice.result_text(self.rec())
        lines = text.split("\n")
        self.assertEqual(lines[0], "*필독 사이트 운영 swap 완료 (11-05 14:30)*")
        self.assertEqual(lines[1], "확인: https://www.pilldoc.co.kr")
        self.assertEqual([l.split()[1] for l in lines[2:]], ["추가", "수정", "변경"])

    def test_failure_and_undo(self):
        self.assertEqual(notice.result_text(self.rec(ok=False))[0],
                         "*필독 사이트 운영 swap 실패 — 운영은 이전 버전 그대로 (11-05 14:30)*\n확인하고 다시 알려 드리겠습니다.")
        undo = {"snapshot": {"label": "필독 사이트", "url": "https://www.pilldoc.co.kr"}}
        self.assertEqual(notice.result_text(self.rec(action="undo", info=undo))[0],
                         "*필독 사이트 swap 되돌림 — 이전 버전으로 운영 중 (11-05 14:30)*\n확인: https://www.pilldoc.co.kr")
        self.assertEqual(notice.result_text(self.rec(action="undo", ok=False, info=undo))[0],
                         "*필독 사이트 swap 되돌리기 실패 — 새 버전으로 운영 중 (11-05 14:30)*\n확인하고 다시 알려 드리겠습니다.")
        self.assertEqual(notice.result_text(self.rec(action="undo", info={}))[0],   # 주소 없는 기록
                         "*pilldocsite swap 되돌림 — 이전 버전으로 운영 중 (11-05 14:30)*")

    def test_old_record_without_snapshot(self):
        text, why = notice.result_text(self.rec(info={"prod": "p", "staging": "s"}))
        self.assertIsNone(text)
        self.assertIn("기능 전", why)
        self.assertIsNone(notice.result_text(None)[0])

    def test_last_record(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "swap-log.jsonl")
            self.assertIsNone(notice.last_record(p, "pilldocsite"))
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps({"repo": "pilldocsite", "id": 1}) + "\n깨진 줄\n")
                f.write(json.dumps({"repo": "adminsite", "id": 2}) + "\n")
                f.write(json.dumps({"repo": "pilldocsite", "id": 3}) + "\n")
            self.assertEqual(notice.last_record(p, "pilldocsite")["id"], 3)


class WhoAmI(unittest.TestCase):
    def test_logged_in(self):
        def run(args, timeout=0):
            return '{"user": "me@x", "sub": "구독"}' if args[0] == "az" else "jw-teoul\n"
        w = swap.whoami(run)
        self.assertEqual(w["az"], {"ok": True, "user": "me@x", "sub": "구독"})
        self.assertEqual(w["gh"], {"ok": True, "user": "jw-teoul"})

    def test_logged_out(self):
        def run(args, timeout=0):
            raise RuntimeError("Please run 'az login'")
        w = swap.whoami(run)
        self.assertFalse(w["az"]["ok"])
        self.assertIn("az login", w["az"]["reason"])
        self.assertIn("gh auth login", w["gh"]["reason"])


class Recheck(unittest.TestCase):
    def test_second_press_refused_while_checking(self):
        r = swap.SwapRunner(lambda: {"repos": [], "resource_group": "rg"}, os.devnull, on_done=lambda _: None)
        calls = []
        r.check_perms = lambda: calls.append("perms")
        self.assertTrue(r.begin_check())
        self.assertTrue(r.status["checking"])
        self.assertFalse(r.begin_check())        # 도는 동안 겹쳐 누름
        self.assertFalse(r.recheck())
        r.run_check(before=lambda: calls.append("accounts"))
        self.assertEqual(calls, ["accounts", "perms"])
        self.assertFalse(r.status["checking"])
        self.assertTrue(r.recheck())             # 끝나면 다시 가능


if __name__ == "__main__":
    unittest.main()
