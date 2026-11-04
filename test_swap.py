"""swap 권한 판정·올리기 대상 판별 테스트(az·ssh 호출 없음). python -m unittest test_swap"""
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
