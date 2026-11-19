// web/weblogin.js 권한 판정이 swap.py 와 같은지(perm_cases.json 공통 사례). node test_weblogin.js
const assert = require("assert");
const W = require("./web/weblogin.js");
const C = require("./perm_cases.json");
const A = "Microsoft.Web/sites/slotsswap/Action";
let n = 0;
for (const c of C.allowed) {
  const [ok, why] = W.swapAllowed(c.perms);
  assert.strictEqual(ok, c.ok, c.name);
  if (c.why) assert.ok(why.includes(c.why), c.name + ": " + why);
  n++;
}
for (const [p, ok] of C.match) { assert.strictEqual(W.actionMatch(p, A), ok, p); n++; }
// 응답 → 권한(모양이 틀리면 허용이 아니라 「확인 실패」, swap.py well_formed 와 같은 규칙)
const OPEN = [{actions: ["Microsoft.Web/sites/*"], notActions: []}];
assert.deepStrictEqual(W.permFromResponse({value: OPEN}).ok, true); n++;
for (const bad of [null, {}, {value: {x: 1}}, {value: [1]}, {value: [[]]}, {value: [{actions: ["*"]}]},
                   {value: [{actions: "*", notActions: []}]}, {value: [{actions: ["*"], notActions: null}]},
                   {value: [{actions: ["*"], notActions: [null]}]}]) {
  const p = W.permFromResponse(bad);
  assert.strictEqual(p.ok, false, JSON.stringify(bad));
  assert.strictEqual(p.error, true, JSON.stringify(bad));
  n++;
}
const denied = W.permFromResponse({value: [{actions: ["*/read"], notActions: []}]});
assert.strictEqual(denied.ok, false); assert.ok(!denied.error); n++;
// 누르기 전 막는 이유 — 「확인 실패」와 「권한 없음」을 가른다
assert.ok(W.permBlock(undefined).includes("조회 전")); n++;
assert.ok(W.permBlock({ok: false, error: true, reason: "토큰"}).includes("권한 확인 실패")); n++;
assert.ok(W.permBlock({ok: false, reason: "역할 없음"}).startsWith("swap 권한 없음")); n++;
assert.strictEqual(W.permBlock({ok: true, reason: "x"}), ""); n++;
// 권한 캐시 — 먼저 시작했지만 늦게 끝난 조회가 더 새 결과를 덮지 않는다(swap.py put_perm 과 같은 규칙, Codex 11-19 4차)
{
  let perms = {};
  const S = W.permStore(() => perms);
  const full = S.ticket(), live = S.ticket();
  assert.ok(S.put("a", {ok: false, reason: "회수"}, live)); n++;
  assert.ok(!S.put("a", {ok: true, reason: "옛 허용"}, full)); n++;
  assert.strictEqual(perms.a.ok, false); n++;
  assert.ok(S.put("b", {ok: true, reason: "x"}, full)); n++;   // 다른 앱은 상관없다
  const old = S.ticket();
  S.reset(); perms = {};   // 로그아웃 — 그 전에 시작한 조회는 새 계정 캐시에 쓰지 않는다
  assert.ok(!S.put("a", {ok: true, reason: "옛 계정"}, old)); n++;
  assert.deepStrictEqual(perms, {}); n++;
  assert.ok(S.put("a", {ok: true, reason: "새 계정"}, S.ticket())); n++;
}
// 실패 정리 때 webJob 기록은 내 것일 때만 지운다(다른 탭의 진행 기록을 지우면 겹친 swap 이 가능해짐)
assert.ok(W.ownsJob({id: "x1"}, {id: "x1"})); n++;
assert.ok(!W.ownsJob({id: "b2"}, {id: "x1"})); n++;
assert.ok(!W.ownsJob(null, {id: "x1"})); n++;
console.log(`weblogin: ${n}건 OK`);
