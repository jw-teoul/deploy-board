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

    def test_commit_only_in_develop(self):
        r = repo(develop_only={"commits": [commit("c7", "PILLDOC-7")]})
        self.assertEqual(self.judge(r)["PILLDOC-7"]["stage"], "develop")

    def test_estimate_prod(self):
        runs = [{"conclusion": "success", "updatedAt": "2026-10-23T10:00:00Z", "headSha": "new"},
                {"conclusion": "failure", "updatedAt": "2026-10-22T09:00:00Z", "headSha": "bad"},
                {"conclusion": "success", "updatedAt": "2026-10-22T07:00:00Z", "headSha": "old"}]
        sha, _ = collector.Collector.estimate_prod(runs, [{"time": "2026-10-22T07:30:00Z", "who": "x"}])
        self.assertEqual(sha, "old")
        self.assertEqual(collector.Collector.estimate_prod(runs, [])[0], None)


if __name__ == "__main__":
    unittest.main()
