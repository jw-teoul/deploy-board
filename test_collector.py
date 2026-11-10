"""단계 판정 최소 단위 테스트: python -m unittest test_collector"""
import unittest

import collector


class FakeGit:
    def __init__(self, ancestors):
        self.ancestors = ancestors  # (a, b) 쌍이 있으면 a 는 b 의 조상

    def is_ancestor(self, a, b):
        return (a, b) in self.ancestors


def repo(**kw):
    base = {"web": "https://github.com/o/r", "branches": [], "prs": [], "develop_only": {"commits": []},
            "staging_only": None, "prod": {"sha": None}}
    base.update(kw)
    return base


def pr(num, base, state, key, merge=None, draft=False):
    return {"number": num, "baseRefName": base, "headRefName": key, "state": state, "isDraft": draft,
            "mergeCommit": merge, "mergedAt": None, "updatedAt": "2026-10-20T00:00:00Z", "url": "", "title": f"{key} t",
            "author": "a", "keys": [key]}


def commit(sha, key, date="2026-10-21T00:00:00Z"):
    return {"sha": sha, "message": f"{key} fix", "author": "a", "date": date, "url": "", "keys": [key]}


class JudgeTest(unittest.TestCase):
    def setUp(self):
        self.c = collector.Collector({"org": "o", "repos": []})

    def judge(self, r, git=None):
        return {i["key"]: i for i in self.c.judge_issues(r, git or FakeGit(set()))}

    def test_branch_only(self):
        r = repo(branches=[{"name": "PILLDOC-1", "sha": "x", "date": "2026-10-01T00:00:00Z", "author": "a", "message": "m", "keys": ["PILLDOC-1"]}])
        self.assertEqual(self.judge(r)["PILLDOC-1"]["stage"], "branch")

    def test_open_pr(self):
        r = repo(prs=[pr(1, "develop", "OPEN", "PILLDOC-2", draft=True)])
        i = self.judge(r)["PILLDOC-2"]
        self.assertEqual(i["stage"], "pr")
        self.assertIn("Draft PR", i["flags"])

    def test_develop_merged(self):
        r = repo(prs=[pr(1, "develop", "MERGED", "PILLDOC-3", merge="m1")], develop_only={"commits": [commit("c1", "PILLDOC-3")]})
        self.assertEqual(self.judge(r)["PILLDOC-3"]["stage"], "develop")

    def test_staging_when_commit_not_in_prod(self):
        r = repo(prs=[pr(1, "deploy/prod", "MERGED", "PILLDOC-4", merge="m4")], prod={"sha": "P"},
                 staging_only={"commits": [commit("c4", "PILLDOC-4")]})
        self.assertEqual(self.judge(r)["PILLDOC-4"]["stage"], "staging")

    def test_prod_when_merge_commit_is_ancestor(self):
        r = repo(prs=[pr(1, "deploy/prod", "MERGED", "PILLDOC-5", merge="m5")], prod={"sha": "P"}, staging_only={"commits": []})
        self.assertEqual(self.judge(r, FakeGit({("m5", "P")}))["PILLDOC-5"]["stage"], "prod")

    def test_prod_then_more_commits_in_staging(self):
        r = repo(prs=[pr(1, "deploy/prod", "MERGED", "PILLDOC-6", merge="m6")], prod={"sha": "P"},
                 staging_only={"commits": [commit("c6", "PILLDOC-6")]})
        i = self.judge(r, FakeGit({("m6", "P")}))["PILLDOC-6"]
        self.assertEqual(i["stage"], "staging")
        self.assertTrue(any("추가 커밋" in f for f in i["flags"]))

    def test_develop_branch_name_from_config(self):
        """adpsapi 처럼 통합 브랜치가 development 인 레포: 그 브랜치 머지가 develop 단계."""
        r = repo(develop_branch="development", prs=[pr(1, "development", "MERGED", "PILLDOC-8", merge="m8")])
        self.assertEqual(self.judge(r)["PILLDOC-8"]["stage"], "develop")

    def test_commit_only_in_develop(self):
        r = repo(develop_only={"commits": [commit("c7", "PILLDOC-7")]})
        self.assertEqual(self.judge(r)["PILLDOC-7"]["stage"], "develop")

    def test_branch_mode_staging_and_prod(self):
        """브랜치 모드(pillcheck): deploy/dev 머지 = 스테이징, deploy/prod 머지 커밋이 운영 SHA 에 있으면 운영."""
        r = repo(staging_branch="deploy/dev", prod_branch="deploy/prod", prod={"sha": "P"}, staging_only={"commits": []},
                 prs=[pr(1, "deploy/dev", "MERGED", "MOBILE-1", merge="m1"), pr(2, "deploy/prod", "MERGED", "MOBILE-2", merge="m2"),
                      pr(3, "deploy/prod", "MERGED", "MOBILE-3", merge="m3")])
        j = self.judge(r, FakeGit({("m2", "P")}))
        self.assertEqual(j["MOBILE-1"]["stage"], "staging")
        self.assertEqual(j["MOBILE-2"]["stage"], "prod")
        self.assertEqual(j["MOBILE-3"]["stage"], "staging")   # 머지됐지만 배포 run 이 아직 성공하지 않음

    def test_prod_from_runs(self):
        runs = [{"conclusion": "failure", "updatedAt": "t2", "headSha": "bad"}, {"conclusion": "success", "updatedAt": "t1", "headSha": "ok"}]
        self.assertEqual(collector.Collector.prod_from_runs(runs)[0], "ok")
        self.assertEqual(collector.Collector.prod_from_runs([])[0], None)

    def test_estimate_prod(self):
        runs = [{"conclusion": "success", "updatedAt": "2026-10-23T10:00:00Z", "headSha": "new"},
                {"conclusion": "failure", "updatedAt": "2026-10-22T09:00:00Z", "headSha": "bad"},
                {"conclusion": "success", "updatedAt": "2026-10-22T07:00:00Z", "headSha": "old"}]
        sha, _ = collector.Collector.estimate_prod(runs, [{"time": "2026-10-22T07:30:00Z", "who": "x"}])
        self.assertEqual(sha, "old")
        self.assertEqual(collector.Collector.estimate_prod(runs, [])[0], None)

    R = staticmethod(lambda t, sha, c="success": {"conclusion": c, "updatedAt": t, "headSha": sha})
    S = staticmethod(lambda t: {"time": t, "who": "x"})

    def test_estimate_after_rollback(self):   # 배포 없이 swap 두 번(되돌리기) → 이전 운영으로 돌아감
        runs = [self.R("2026-11-01T02:00:00Z", "B"), self.R("2026-11-01T01:00:00Z", "A")]
        swaps = [self.S("2026-11-01T04:00:00Z"), self.S("2026-11-01T03:00:00Z"), self.S("2026-11-01T01:30:00Z")]
        sha, basis = collector.Collector.estimate_prod(runs, swaps)
        self.assertEqual(sha, "A")                 # A 운영 → B 배포·swap(운영 B) → 되돌리기(운영 A)
        self.assertIn("추적", basis); self.assertNotIn("범위 밖", basis)
        self.assertEqual(collector.Collector.estimate_prod(runs, swaps[1:])[0], "B")

    def test_estimate_rollback_out_of_range_falls_back(self):   # 되돌리기가 조회 범위 밖 운영을 가리킴
        runs = [self.R("2026-11-01T02:00:00Z", "B")]
        swaps = [self.S("2026-11-01T04:00:00Z"), self.S("2026-11-01T03:00:00Z")]
        sha, basis = collector.Collector.estimate_prod(runs, swaps)
        self.assertEqual(sha, "B"); self.assertIn("추적 범위 밖 — 단순 추정", basis)

    def test_estimate_failed_run_does_not_enter_staging(self):
        runs = [self.R("2026-11-01T02:00:00Z", "BAD", "failure"), self.R("2026-11-01T01:00:00Z", "A")]
        self.assertEqual(collector.Collector.estimate_prod(runs, [self.S("2026-11-01T03:00:00Z")])[0], "A")

    def test_estimate_pending_rollback(self):   # 잠정 swap 도 같은 추적 — 잠정 되돌리기면 이전 운영
        runs = [self.R("2026-11-01T02:00:00Z", "B"), self.R("2026-11-01T01:00:00Z", "A")]
        act = [self.S("2026-11-01T03:00:00Z"), self.S("2026-11-01T01:30:00Z")]
        pend = {"time": "2026-11-01T03:05:00.000000Z", "from": "staging", "to": "Production"}
        self.assertEqual(collector.Collector.estimate_prod(runs, [pend] + act)[0], "A")


    def test_estimate_ignores_runs_before_lookback(self):   # 조회 기간 밖 swap 을 모르는 오래된 run 은 추적에 안 넣음
        import datetime as dt
        runs = [self.R("2026-11-01T04:30:00Z", "B"), self.R("2026-07-01T01:00:00Z", "OLD")]
        swaps = [self.S("2026-11-01T03:00:00Z")]   # 7~10월 사이 swap 은 조회 기간 밖이라 모름
        since = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        self.assertNotIn("단순 추정", collector.Collector.estimate_prod(runs, swaps)[1])   # 넣으면 확신 문구로 추적
        sha, basis = collector.Collector.estimate_prod(runs, swaps, since)
        self.assertEqual(sha, "OLD"); self.assertIn("추적 범위 밖 — 단순 추정", basis)

    def test_pending_not_double_counted_when_only_start_event(self):   # 검토 재현: 시작 이벤트만 있고 처리 이벤트 2분 뒤
        runs = [self.R("2026-11-01T02:00:00Z", "B"), self.R("2026-11-01T01:00:00Z", "A")]
        act = collector.Collector.merge_swap_events([
            {"time": "2026-11-01T03:00:00Z", "who": "u@x", "cid": "c2"},
            {"time": "2026-11-01T01:30:00Z", "who": "u@x", "cid": "c1"}, {"time": "2026-11-01T01:31:00Z", "who": "P", "cid": "c1"}])
        site = {"time": "2026-11-01T03:01:55.000000Z", "from": "staging", "to": "Production"}
        pend = collector.Collector.pending_swap(site, act)
        self.assertIsNone(pend)
        self.assertEqual(collector.Collector.estimate_prod(runs, act)[0], "B")
        later = dict(site, time="2026-11-01T03:12:00.000000Z")               # 10분 넘게 뒤면 다음 swap(잠정)
        self.assertIsNotNone(collector.Collector.pending_swap(later, act))


class SwapMergeTest(unittest.TestCase):
    M = staticmethod(collector.Collector.merge_swap_events)

    def test_same_correlation_two_minutes_apart_is_one(self):   # adminsite 11-04 01:38:15/01:40:18 실측 모양
        ev = [{"time": "2026-11-04T01:38:15Z", "who": "u@x", "cid": "c1"},
              {"time": "2026-11-04T01:40:18Z", "who": "SlotSwapJobProcessor", "cid": "c1"}]
        self.assertEqual(self.M(ev), [{"time": "2026-11-04T01:40:18Z", "who": "u@x", "start": "2026-11-04T01:38:15Z", "n": 2}])

    def test_different_correlation_close_together_are_two(self):
        ev = [{"time": "2026-11-04T01:00:00Z", "who": "u@x", "cid": "c1"},
              {"time": "2026-11-04T01:00:30Z", "who": "v@x", "cid": "c2"}]
        self.assertEqual([e["who"] for e in self.M(ev)], ["v@x", "u@x"])

    def test_no_correlation_does_not_chain(self):   # 2분 간격 셋: 첫 시각 기준이라 1·2 는 합치고 3 은 따로
        ev = [{"time": "2026-11-04T01:00:00Z", "who": "a@x"}, {"time": "2026-11-04T01:02:00Z", "who": "P"},
              {"time": "2026-11-04T01:04:00Z", "who": "P"}]
        self.assertEqual([e["time"] for e in self.M(ev)], ["2026-11-04T01:04:00Z", "2026-11-04T01:02:00Z"])




class MergeTest(unittest.TestCase):
    def test_merge_replaces_only_collected_repo(self):
        import json, os, tempfile
        old = {"collected_at": "t0", "duration_s": 40, "errors": ["adminsite: x", "pilldocsite: y"],
               "repos": [{"name": "adminsite", "v": 1}, {"name": "pilldocsite", "v": 1}, {"name": "agentapiv2", "v": 1}]}
        fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(old, f)
        new = {"collected_at": "t1", "duration_s": 12, "errors": [], "repos": [{"name": "pilldocsite", "v": 2}]}
        m = collector.merge_state(new, path)
        os.remove(path)
        self.assertEqual([r["name"] for r in m["repos"]], ["adminsite", "pilldocsite", "agentapiv2"])
        self.assertEqual({r["name"]: r["v"] for r in m["repos"]}["pilldocsite"], 2)
        self.assertEqual(m["collected_at"], "t1")
        self.assertEqual(m["errors"], ["adminsite: x"])


class PendingSwapTest(unittest.TestCase):
    """사이트 slotSwapStatus 로 만드는 잠정 swap."""
    P = staticmethod(collector.Collector.pending_swap)
    act = [{"time": "2026-11-05T05:09:43.3408237Z", "who": "u@x"}]

    def test_newer_than_activity_log_is_pending(self):
        site = {"time": "2026-11-05T06:00:00.000000Z", "from": "staging", "to": "Production"}
        self.assertEqual(self.P(site, self.act)["time"], site["time"])

    def test_same_swap_as_activity_log_is_not_pending(self):   # 11-05 실측: 사이트 값이 7초 앞섬
        site = {"time": "2026-11-05T05:09:36.189000Z", "from": "staging", "to": "Production"}
        self.assertIsNone(self.P(site, self.act))

    def test_non_production_target_ignored(self):
        site = {"time": "2026-11-05T06:00:00.000000Z", "from": "production", "to": "staging"}
        self.assertIsNone(self.P(site, self.act))

    def test_quick_second_swap_is_pending(self):   # 앞 swap 기록 50초 뒤의 되돌리기 — 앞 기록으로 오인하지 않는다
        site = {"time": "2026-11-05T05:10:33.000000Z", "from": "staging", "to": "Production"}
        self.assertIsNotNone(self.P(site, self.act))

    def test_site_swaps_skips_bad_item_and_caches_failure(self):
        rows = [{"name": "A", "s": {"timestampUtc": "깨진 값"}}, {"name": "N", "s": {"timestampUtc": "2026-11-05T05:00:00"}},
                {"name": "B", "s": {"timestampUtc": "2026-11-05T05:00:00Z", "destinationSlotName": "Production"}}]
        orig = collector.az_json
        try:
            collector.az_json = lambda args, timeout=None: rows
            self.assertEqual(list(collector.Collector({"org": "o", "resource_group": "rg", "repos": []}).site_swaps()), ["B"])
            calls = []
            def boom(args, timeout=None):
                calls.append(1); raise RuntimeError("az 실패")
            collector.az_json = boom
            c = collector.Collector({"org": "o", "resource_group": "rg", "repos": []})
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    c.site_swaps()
            self.assertEqual(len(calls), 1)
        finally:
            collector.az_json = orig

    def test_missing_values(self):
        self.assertIsNone(self.P(None, self.act))
        self.assertIsNone(self.P({"time": None, "to": "Production"}, self.act))
        self.assertIsNotNone(self.P({"time": "2026-11-05T06:00:00Z", "to": "Production"}, []))   # 활동 기록이 비어도 잠정

    def test_estimate_uses_pending_time(self):
        runs = [{"conclusion": "success", "updatedAt": "2026-11-05T05:30:00Z", "headSha": "new"},
                {"conclusion": "success", "updatedAt": "2026-11-05T04:00:00Z", "headSha": "old"}]
        pend = self.P({"time": "2026-11-05T06:00:00Z", "to": "Production"}, self.act)
        self.assertEqual(collector.Collector.estimate_prod(runs, self.act)[0], "old")
        self.assertEqual(collector.Collector.estimate_prod(runs, [pend] + self.act)[0], "new")

    def test_site_swaps_parses_and_caches(self):
        rows = [{"name": "A", "s": {"timestampUtc": "2026-11-05T05:09:36.189000+00:00", "sourceSlotName": "staging",
                                   "destinationSlotName": "Production"}},
                {"name": "B", "s": None}]
        calls = []
        orig = collector.az_json
        collector.az_json = lambda args, timeout=None: calls.append(args) or rows
        try:
            c = collector.Collector({"org": "o", "resource_group": "rg", "repos": []})
            got = c.site_swaps(); c.site_swaps()
        finally:
            collector.az_json = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(got, {"A": {"time": "2026-11-05T05:09:36.189000Z", "from": "staging", "to": "Production"}})


class WatcherTest(unittest.TestCase):
    """감시 스레드의 tick: 지문이 바뀐 레포만 refresh(only=[...]) 로 넘기는지."""

    def setUp(self):
        import server
        self.server = server
        self.cfg = {"org": "o", "repos": [{"name": "a", "app": "A"}, {"name": "b", "app": "B"}, {"name": "c"}]}
        self.probes = {n: {"refs": "1", "pr": "p"} for n in "abc"}
        self.site = {"A": {"time": "s1"}, "B": {"time": "s1"}}
        self.act = {"A": None, "B": None}
        self.state = {"repos": []}
        self.calls, self.act_calls = [], []
        self.orig = (collector.load_cfg, collector.Collector.probe_repo, collector.Collector.probe_swap,
                     collector.Collector.site_swaps, server.refresh, server.load_state)
        collector.load_cfg = lambda path=None: self.cfg
        collector.Collector.probe_repo = lambda s, rc: dict(self.probes[rc["name"]])
        collector.Collector.probe_swap = lambda s, app: self.act_calls.append(app) or self.act[app]
        collector.Collector.site_swaps = lambda s: self.site
        server.refresh = lambda only=None: self.calls.append(only)
        server.load_state = lambda: self.state

    def tearDown(self):
        (collector.load_cfg, collector.Collector.probe_repo, collector.Collector.probe_swap,
         collector.Collector.site_swaps, self.server.refresh, self.server.load_state) = self.orig

    def test_tick_refreshes_only_changed(self):
        w = self.server.Watcher(30, 60)
        w.tick(); self.assertEqual(self.calls, [])                 # 첫 tick 은 기준만 잡는다
        self.probes["b"]["refs"] = "2"; w.tick(); self.assertEqual(self.calls, [["b"]])
        w.tick(); self.assertEqual(self.calls, [["b"]])              # 변화 없으면 안 부른다
        w.last_swap_check = 0; self.site["A"] = {"time": "s2"}; w.tick(); self.assertEqual(self.calls, [["b"], ["a"]])
        self.assertEqual(self.server.status["watch"]["changes"]["a"]["why"], "swap")
        self.assertEqual(self.act_calls, [])                         # 잠정이 없으면 활동 기록은 안 본다

    def test_site_lookup_failure_keeps_repo_watch(self):
        w = self.server.Watcher(30, 60)
        w.tick()
        def boom(s):
            raise RuntimeError("az 실패")
        collector.Collector.site_swaps = boom
        w.last_swap_check = 0; self.probes["a"]["refs"] = "2"; w.tick()
        self.assertEqual(self.calls, [["a"]])                        # 레포 감시는 계속
        self.assertIn("az 실패", self.server.status["watch"]["probe_error"])
        self.assertEqual(w.swap["a"], "s1")       # 실패를 swap 변화로 보지 않는다

    def test_pending_confirmed_by_activity_log(self):
        import datetime as dt
        t = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        ts = lambda d: (t + dt.timedelta(seconds=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.site["A"] = {"time": ts(-60)}
        w = self.server.Watcher(30, 60)
        w.tick()
        self.state = {"repos": [{"name": "a", "pending_swap": {"time": ts(-60)}}]}
        self.act["A"] = ts(-3600)                                    # 활동 기록엔 아직 옛 swap
        w.last_swap_check = 0; w.tick()
        self.assertEqual(self.calls, []); self.assertEqual(self.act_calls, ["A"])
        self.act["A"] = ts(-53)                                      # 확정이 올라옴(사이트 값보다 7초 뒤)
        w.last_swap_check = 0; w.tick()
        self.assertEqual(self.calls, [["a"]])
        self.assertEqual(self.server.status["watch"]["changes"]["a"]["why"], "swap 확정")

    def test_confirm_due_window(self):
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        f = self.server.Watcher.confirm_due
        import time as _t
        ago = lambda **k: {"time": (now - dt.timedelta(**k)).isoformat()}
        self.assertFalse(f(None)); self.assertFalse(f({"time": "이상한 값"}))
        self.assertTrue(f(ago(minutes=5), _t.time()))                 # 15분 안: 매번
        self.assertFalse(f(ago(minutes=20), _t.time()))               # 그 뒤: 10분마다
        self.assertTrue(f(ago(minutes=20), _t.time() - 601))
        self.assertFalse(f(ago(hours=7), 0))                          # 6시간 뒤: 그만

    def test_missed_swap_at_start_triggers_refresh(self):
        self.state = {"repos": [{"name": "a", "swaps": [{"time": "2026-11-05T01:00:00Z"}]}, {"name": "b", "swaps": []}]}
        self.site = {"A": {"time": "2026-11-05T05:00:00.000000Z", "to": "Production"}}   # state 보다 새 swap
        w = self.server.Watcher(30, 60)
        w.tick()
        self.assertEqual(self.calls, [["a"]])
        self.assertEqual(self.server.status["watch"]["changes"]["a"]["why"], "swap(시작 보정)")

    def test_confirm_failure_keeps_other_changes(self):
        import datetime as dt
        t = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        w = self.server.Watcher(30, 60)
        w.tick()
        self.state = {"repos": [{"name": "b", "pending_swap": {"time": t}}]}
        def boom(s, app):
            raise RuntimeError("활동 기록 실패")
        collector.Collector.probe_swap = boom
        self.probes["a"]["refs"] = "2"; w.last_swap_check = 0; w.tick()
        self.assertEqual(self.calls, [["a"]])
        self.assertIn("활동 기록 실패", self.server.status["watch"]["probe_error"])


    def test_failed_refresh_is_retried_after_interval(self):
        w = self.server.Watcher(30, 60)
        w.tick()
        results = [["a"], []]
        self.server.refresh = lambda only=None: self.calls.append(only) or results.pop(0)
        self.probes["a"]["refs"] = "2"; w.tick()
        self.assertEqual(self.calls, [["a"]]); self.assertIn("a", w.retry)
        w.tick(); self.assertEqual(self.calls, [["a"]])                 # 5분 전에는 다시 안 함
        w.retry["a"] = (w.retry["a"][0], 0, 1); w.tick()                # 시각이 지나면 다시 수집
        self.assertEqual(self.calls, [["a"], ["a"]])
        self.assertEqual(self.server.status["watch"]["changes"]["a"]["why"], "재시도(refs)")
        self.assertNotIn("a", w.retry)                                   # 성공하면 그만
        w.tick(); self.assertEqual(len(self.calls), 2)

    def test_refresh_busy_counts_as_failed(self):
        w = self.server.Watcher(30, 60)
        w.tick()
        self.server.refresh = lambda only=None: None                     # 다른 수집이 먼저 잡음
        self.probes["b"]["pr"] = "q"; w.tick()
        self.assertEqual(w.retry["b"][0], "pr")

    def test_retry_backoff_keeps_reason(self):
        import time as _t
        w = self.server.Watcher(30, 60)
        w.tick()
        self.server.refresh = lambda only=None: self.calls.append(only) or list(only)   # 계속 실패
        self.probes["a"]["refs"] = "2"; w.tick()
        waits = []
        for _ in range(6):
            waits.append(round(w.retry["a"][1] - _t.time(), -1))
            w.retry["a"] = (w.retry["a"][0], 0, w.retry["a"][2]); w.tick()
        self.assertEqual(waits, [300, 600, 1200, 2400, 3600, 3600])
        self.assertEqual(w.retry["a"][0], "refs")


if __name__ == "__main__":
    unittest.main()
