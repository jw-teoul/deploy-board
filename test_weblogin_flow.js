// web/weblogin.js 웹 로그인 swap 흐름 — 브라우저·MSAL·ARM 을 가짜로 두고 돌린다(실제 로그인·swap 없음). node test_weblogin_flow.js
// 확인: 조회 실패는 「확인 실패」(error), 누르는 순간 재확인이 막으면 POST 를 안 보냄, 실패 정리가 다른 탭의 webJob 을 지우지 않음,
//       늦게 끝난 전체 조회가 더 새 재확인 결과를 덮지 않음.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const SRC = fs.readFileSync(__dirname + "/web/weblogin.js", "utf8");
const OPEN = [{actions: ["Microsoft.Web/sites/*"], notActions: []}];
const NONE = [{actions: ["*/read"], notActions: []}];

function boot() {
  const store = new Map();
  const arm = {perms: {}, posts: [], hold: null};   // perms: 앱 → value 배열 | Error | () => Promise
  const reply = (status, body) => ({status, ok: status < 400, json: async () => body, headers: {get: () => null}});
  const ctx = {
    console, setTimeout, clearTimeout, Promise, Date, JSON, Math, Error,
    location: {hash: "", search: "", hostname: "localhost", port: "8765", origin: "http://localhost:8765"},
    localStorage: {getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k)},
    document: {head: {appendChild: s => setTimeout(() => s.onload && s.onload(), 0)}, createElement: () => ({})},
    fetch: async (url) => {
      const m = /\/sites\/([^/?]+)\/providers\/Microsoft\.Authorization\/permissions/.exec(url);
      if (m) {
        let v = arm.perms[m[1]];
        if (typeof v === "function") v = await v();
        if (v instanceof Error) return reply(403, {error: {message: v.message}});
        return reply(200, {value: v || OPEN});
      }
      if (/slotsswap/.test(url)) { arm.posts.push(url); return reply(200, {}); }
      if (/\/sites\/[^/?]+\?api-version/.test(url)) return reply(200, {properties: {slotSwapStatus: {timestampUtc: "2026-11-19T00:00:00Z"}}});
      throw new Error("예상 밖 주소 " + url);
    },
  };
  ctx.window = ctx;
  ctx.window.parent = ctx.window;
  class InteractionRequiredAuthError extends Error {}
  const acct = {homeAccountId: "h1", username: "me@x"};
  ctx.msal = {InteractionRequiredAuthError, createStandardPublicClientApplication: async () => ({
    handleRedirectPromise: async () => ({account: acct}), getActiveAccount: () => acct, getAllAccounts: () => [acct],
    setActiveAccount() {}, acquireTokenSilent: async () => ({accessToken: "T"}), clearCache: async () => {},
  })};
  vm.createContext(ctx);
  const W = vm.runInContext(SRC + "\nWEBLOGIN", ctx);
  return {W, arm, store};
}
const tick = () => new Promise(r => setTimeout(r, 5));
async function ready(W) {
  await W.init({repos: [{name: "a", app: "A"}, {name: "b", app: "B"}], azure: {subscription_id: "s", resource_group: "rg"},
                entra: {client_id: "c", tenant_id: "t", msal: "m.js"}});
  for (let i = 0; i < 50 && (W.sw.checking || !W.sw.perms_checked_at); i++) await tick();
}

const cases = [];
const it = (name, fn) => cases.push([name, fn]);

it("조회 실패는 「권한 없음」이 아니라 「확인 실패」", async () => {
  const {W, arm} = boot();
  arm.perms.A = new Error("AuthorizationFailed");
  arm.perms.B = NONE;
  await ready(W);
  assert.strictEqual(W.sw.perms.a.error, true);
  assert.ok(W.permBlock(W.sw.perms.a).includes("권한 확인 실패"));
  assert.ok(!W.sw.perms.b.error && !W.sw.perms.b.ok);
  const [ok, why] = await W.start("a", "swap");
  assert.strictEqual(ok, false); assert.ok(why.includes("권한 확인 실패"), why);
  assert.strictEqual(arm.posts.length, 0);
});

it("허용 캐시여도 누르는 순간 다시 물어 막히면 POST 를 보내지 않는다", async () => {
  const {W, arm, store} = boot();
  await ready(W);
  assert.strictEqual(W.sw.perms.a.ok, true);
  arm.perms.A = NONE;   // 그 사이 역할 회수
  const [ok, why] = await W.start("a", "swap");
  assert.strictEqual(ok, false); assert.ok(why.includes("지금 다시 확인해 보니"), why);
  assert.strictEqual(arm.posts.length, 0);
  assert.strictEqual(W.sw.perms.a.ok, false);   // 버튼도 꺼진다
  assert.strictEqual(W.sw.job, null);
  assert.ok(![...store.keys()].some(k => k.startsWith("webJob")));
});

it("재확인이 통과하면 POST 를 한 번 보낸다", async () => {
  const {W, arm} = boot();
  await ready(W);
  const [ok, why] = await W.start("a", "swap");
  assert.strictEqual(ok, true, why);
  assert.strictEqual(arm.posts.length, 1);
});

it("실패 정리가 다른 탭이 그 사이 기록한 webJob 을 지우지 않는다", async () => {
  const {W, arm, store} = boot();
  await ready(W);
  let release;
  arm.perms.A = () => new Promise(r => { release = () => r(NONE); });
  const p = W.start("a", "swap");
  await tick();
  const other = {id: "tabB", repo: "b", started_at: new Date().toISOString()};
  store.set("webJob:h1", JSON.stringify(other));   // 다른 탭이 swap 시작
  release();
  const [ok] = await p;
  assert.strictEqual(ok, false);
  assert.deepStrictEqual(JSON.parse(store.get("webJob:h1")), other);
  assert.strictEqual(arm.posts.length, 0);
});

it("기다리는 사이 다른 탭이 시작했으면 이 탭은 POST 하지 않는다", async () => {
  const {W, arm, store} = boot();
  await ready(W);
  let release;
  arm.perms.A = () => new Promise(r => { release = () => r(OPEN); });
  const p = W.start("a", "swap");
  await tick();
  store.set("webJob:h1", JSON.stringify({id: "tabB", repo: "b", started_at: new Date().toISOString()}));
  release();
  const [ok, why] = await p;
  assert.strictEqual(ok, false); assert.ok(why.includes("다른 화면"), why);
  assert.strictEqual(arm.posts.length, 0);
  assert.strictEqual(JSON.parse(store.get("webJob:h1")).id, "tabB");
});

it("늦게 끝난 전체 조회가 더 새 재확인 결과(권한 없음)를 덮지 않는다", async () => {
  const {W, arm} = boot();
  await ready(W);
  let release;
  arm.perms.A = () => new Promise(r => { release = () => r(OPEN); });
  const full = W.checkPerms();   // 전체 조회 시작(허용 응답이 늦게 옴)
  await tick();
  arm.perms.A = NONE;
  const [ok] = await W.start("a", "swap");
  assert.strictEqual(ok, false);
  release();
  await full;
  assert.strictEqual(W.sw.perms.a.ok, false);
});

(async () => {
  for (const [name, fn] of cases) {
    try { await fn(); } catch (e) { console.error("실패: " + name); throw e; }
  }
  console.log(`weblogin 흐름: ${cases.length}건 OK`);
})().catch(e => { console.error(e); process.exit(1); });
