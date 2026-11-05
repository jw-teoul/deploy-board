/* 웹 로그인 swap — 보드를 연 사람이 Microsoft 계정으로 로그인하면, 그 사람의 Azure 권한으로 브라우저가 ARM 을 직접 부른다.
 * - 토큰은 이 브라우저 탭(sessionStorage, MSAL)에만 있고 보드 서버로 보내지 않는다. 서버는 공개 설정(tenant·client ID)만 준다.
 * - 권한 판정은 swap.py swap_allowed 와 같은 규칙(test_weblogin.js 가 같은 사례로 확인).
 * - swap 은 포털·az 와 같은 ARM 호출이라 Azure 활동 기록에 로그인한 사람 이름으로 남는다.
 * 보드 서버가 swap 모드(--allow-swap, 서버의 az 로 swap)면 서버가 entra 설정을 주지 않아 이 기능은 꺼진다.
 */
"use strict";
const WEBLOGIN = (function () {
  const ARM = "https://management.azure.com";
  const SCOPES = [ARM + "/user_impersonation"];
  const ACTION = "Microsoft.Web/sites/slotsswap/Action";
  const UNDO_MIN = 30, POLL_MS = 5000, TIMEOUT_MS = 15 * 60000;

  // ---------- 권한 판정(swap.py 와 같은 규칙) ----------
  function actionMatch(pattern, action) {
    const rx = "^" + String(pattern).split("*").map(p => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$";
    return new RegExp(rx, "i").test(action);
  }
  // permissions API 의 value 목록 → [허용?, 이유]. 항목(역할)마다 actions 에 맞고 그 항목의 notActions 에 안 걸리면 허용.
  function swapAllowed(perms, action = ACTION) {
    let blockedBy = null;
    for (const p of perms || []) {
      const allow = (p.actions || []).find(a => actionMatch(a, action));
      if (!allow) continue;
      const deny = (p.notActions || []).find(n => actionMatch(n, action));
      if (deny) { blockedBy = blockedBy || `'${allow}' 역할이 '${deny}' 를 제외함`; continue; }
      return [true, `'${allow}' 로 허용`];
    }
    return [false, blockedBy || "swap 을 허용하는 역할 없음"];
  }
  if (typeof window === "undefined") return {actionMatch, swapAllowed};   // node 시험용

  // 로그인 갱신용 숨은 iframe·팝업 안에서 이 화면이 로그인 응답으로 열린 경우: 응답을 원래 화면에 넘기고 보드는 띄우지 않는다.
  const q = location.hash + location.search;
  if (/[#?&](code|error)=/.test(q) && /[#?&]state=/.test(q) && window.parent !== window) {   // loginRedirect 만 쓰므로 팝업(opener)은 보지 않는다
    window.AUTH_FRAME = true;
    const s = document.createElement("script");
    s.src = "vendor/msal-redirect-bridge.min.js";
    s.onload = () => window.msalRedirectBridge.broadcastResponseToMainFrame().catch(() => {});
    document.head.appendChild(s);
    return {actionMatch, swapAllowed};
  }

  const W = {
    available: false, ready: false, account: null, error: null, pca: null, cfg: null, azure: null, repos: [],
    onChange: () => {}, toast: () => {},
    // 보드의 swap 버튼이 읽는 모양(서버 swap 모드의 status.swap 과 같은 키)
    sw: {enabled: false, web: true, perms: {}, perms_checked_at: null, checking: false, job: null, last: null, undo: {}, pending: {}, undo_minutes: UNDO_MIN, resource_group: null},
  };
  const changed = () => { try { W.onChange(); } catch (e) { console.error(e); } };
  const msg = e => String(e?.errorMessage || e?.message || e).slice(0, 300);
  const now = () => new Date().toISOString();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const siteId = app => `/subscriptions/${W.azure.subscription_id}/resourceGroups/${W.azure.resource_group}/providers/Microsoft.Web/sites/${app}`;
  const loadScript = src => new Promise((ok, bad) => {
    const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = () => bad(new Error("MSAL 파일을 못 읽음")); document.head.appendChild(s);
  });

  // ---------- 되돌리기(이 브라우저에서 30분 안에 한 swap 만)·진행 중 작업·반영 대기 — 같은 브라우저의 다른 탭과 나눈다 ----------
  const key = k => k + ":" + (W.account?.homeAccountId || "");
  const undoKey = () => key("webUndo");
  const getLS = k => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } };
  const setLS = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); } catch {} };
  function loadUndo() {
    const u = getLS(undoKey()) || {}, p = getLS(key("webPending")) || {};
    for (const k of Object.keys(u)) if (!(new Date(u[k].until) > Date.now())) delete u[k];          // 만료된 되돌리기는 지운다
    for (const k of Object.keys(p)) if (Date.now() - new Date(p[k].finished_at) > 6 * 3600e3) delete p[k];   // 반영 대기는 6시간까지
    W.sw.undo = u; W.sw.pending = p; saveUndo();
  }
  function saveUndo() {
    setLS(undoKey(), Object.keys(W.sw.undo).length ? W.sw.undo : null);
    setLS(key("webPending"), Object.keys(W.sw.pending).length ? W.sw.pending : null);
  }
  // 다른 탭·새로고침 전 화면에서 시작한 swap(최대 15분) — 겹쳐 실행하지 않게 localStorage 로 나눈다
  function otherJob() { const j = getLS(key("webJob")); return j && Date.now() - new Date(j.started_at) < TIMEOUT_MS + 60000 ? j : null; }
  function undoOpen(name) { const u = W.sw.undo[name]; return !!u && new Date(u.until) > Date.now(); }

  // ---------- 로그인 ----------
  async function init(state) {
    W.repos = (state?.repos || []).filter(r => r.app).map(r => ({name: r.name, app: r.app}));
    if (state?.azure?.subscription_id) { W.azure = state.azure; W.sw.resource_group = state.azure.resource_group; }
    if (W.available || !state?.entra || !W.azure) return;
    W.available = true; W.cfg = state.entra;
    if (location.hostname === "127.0.0.1" || location.hostname === "[::1]") {   // 등록한 리디렉션 주소는 localhost·개발 서버 주소뿐
      W.error = `이 주소로는 로그인할 수 없음 — http://localhost:${location.port}/ 로 접속`;
      changed(); return;
    }
    changed();
    try {
      await loadScript(W.cfg.msal);
      W.pca = await window.msal.createStandardPublicClientApplication({
        auth: {clientId: W.cfg.client_id, authority: `https://login.microsoftonline.com/${W.cfg.tenant_id}`,
               redirectUri: location.origin + "/", postLogoutRedirectUri: location.origin + "/"},
        cache: {cacheLocation: "sessionStorage"},
      });
      const res = await W.pca.handleRedirectPromise();
      W.account = res?.account || W.pca.getActiveAccount() || W.pca.getAllAccounts()[0] || null;
      if (W.account) { W.pca.setActiveAccount(W.account); signedIn(); }
    } catch (e) { W.error = "로그인 준비 실패 — " + msg(e); }
    W.ready = true;
    changed();
  }
  function signedIn() {
    W.sw.enabled = true; W.error = null; loadUndo();
    resume();
    checkPerms();
  }
  async function login() {
    try { await W.pca.loginRedirect({scopes: SCOPES, prompt: "select_account"}); }
    catch (e) { W.error = "로그인 실패 — " + msg(e); changed(); }
  }
  async function logout() {   // 이 브라우저의 토큰만 지운다(Microsoft 계정 자체 로그아웃은 하지 않음)
    if (W.sw.job) { W.toast("swap 진행 중에는 로그아웃하지 않습니다", true); return; }
    try { await W.pca.clearCache({account: W.account}); } catch {}
    W.account = null; W.sw.enabled = false; W.sw.perms = {}; W.sw.perms_checked_at = null; W.sw.undo = {}; W.sw.pending = {};
    changed();
  }
  // interactive=false: swap 을 기다리는 중에는 화면을 떠나지 않는다(떠나면 결과·되돌리기 기록을 잃음)
  async function token(interactive = true) {
    try { return (await W.pca.acquireTokenSilent({scopes: SCOPES, account: W.account})).accessToken; }
    catch (e) {
      if (interactive && e instanceof window.msal.InteractionRequiredAuthError) {   // 로그인이 만료됨 → 다시 로그인(화면이 이동한다)
        await W.pca.acquireTokenRedirect({scopes: SCOPES, account: W.account});
      }
      throw e;
    }
  }
  async function arm(t, method, url, body) {
    if (url.startsWith("http") && !url.startsWith(ARM + "/")) throw new Error("ARM 이 아닌 주소: " + url.slice(0, 80));   // 토큰을 다른 곳에 보내지 않는다
    const res = await fetch(url.startsWith("http") ? url : ARM + url, {method, cache: "no-store",
      headers: {Authorization: "Bearer " + t, ...(body ? {"Content-Type": "application/json"} : {})}, body: body ? JSON.stringify(body) : undefined});
    return res;
  }
  async function armJson(t, url) {
    const res = await arm(t, "GET", url);
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error?.message || `HTTP ${res.status}`);
    return j;
  }

  // ---------- 권한 조회(앱마다 동시에) ----------
  async function checkPerms() {
    if (!W.account || W.sw.checking) return false;
    W.sw.checking = true; changed();
    try {
      const t = await token();
      await Promise.all(W.repos.map(async r => {
        try {
          const j = await armJson(t, `${siteId(r.app)}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01`);
          const [ok, reason] = swapAllowed(j.value);
          W.sw.perms[r.name] = {ok, reason};
        } catch (e) { W.sw.perms[r.name] = {ok: false, reason: "권한 조회 실패 — " + msg(e)}; }
      }));
      W.sw.perms_checked_at = now();
    } catch (e) {
      for (const r of W.repos) W.sw.perms[r.name] = {ok: false, reason: "로그인 토큰을 못 받음 — " + msg(e)};
    } finally { W.sw.checking = false; changed(); }
    return true;
  }

  // ---------- swap·되돌리기 ----------
  // 마지막으로 끝난 swap 의 시각(운영 사이트의 slotSwapStatus) — 완료 판정의 대안(Location 헤더를 못 읽을 때)
  async function swapStamp(t, site) {
    try { return (await armJson(t, `${site}?api-version=2023-12-01`)).properties?.slotSwapStatus?.timestampUtc || null; }
    catch { return null; }
  }
  // → [시작했나, 이유]. 시작하면 끝날 때까지 뒤에서 기다리고 결과를 토스트로 알린다.
  async function start(name, action) {
    const r = W.repos.find(x => x.name === name);
    if (!r || !W.azure) return [false, "swap 모드 레포가 아님"];
    if (action !== "swap" && action !== "undo") return [false, "알 수 없는 동작"];
    const perm = W.sw.perms[name];
    if (!perm?.ok) return [false, "swap 권한 없음 또는 조회 전" + (perm ? " — " + perm.reason : "")];
    if (action === "undo" && !undoOpen(name)) return [false, `되돌릴 swap 이 없음(이 브라우저에서 ${UNDO_MIN}분 안에 한 swap 만)`];
    if (W.sw.job) return [false, `다른 swap 진행 중(${W.sw.job.repo})`];
    const other = otherJob();
    if (other) return [false, `이 브라우저의 다른 화면에서 swap 진행 중(${other.repo}, 최대 ${TIMEOUT_MS / 60000}분)`];
    const job = {id: Math.random().toString(16).slice(2, 10), repo: name, app: r.app, action, started_at: now(), by: W.account?.username};
    W.sw.job = job; changed();
    const site = siteId(r.app);
    let res, before, t;
    try {
      t = await token();
      before = await swapStamp(t, site);
      if (action === "undo" && W.sw.undo[name]?.stamp && before && before !== W.sw.undo[name].stamp)
        throw new Error("그 사이 다른 swap 이 있었음 — 되돌리면 그 swap 을 뒤집으므로 포털에서 확인");
      job.before = before;
      setLS(key("webJob"), job);
      res = await arm(t, "POST", `${site}/slots/staging/slotsswap?api-version=2023-12-01`, {targetSlot: "production", preserveVnet: true});
      if (res.status !== 200 && res.status !== 202) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.error?.message || `HTTP ${res.status}`);
      }
    } catch (e) {
      W.sw.job = null; setLS(key("webJob"), null); changed();
      return [false, msg(e)];
    }
    let loc = null;
    if (res.status === 202) {
      const op = res.headers.get("Azure-AsyncOperation"), l = res.headers.get("Location");
      loc = op ? {url: op, async: true} : l ? {url: l, async: false} : null;
    }
    (res.status === 200 ? Promise.resolve(null) : wait(site, loc, job)).then(err => finish(job, err));
    return [true, "시작"];
  }
  // 새로고침·다른 탭 전에 시작한 swap 이 남아 있으면 이어서 기다린다(작업 주소는 모르므로 swap 시각으로 판정)
  function resume() {
    const j = otherJob();
    if (!j || W.sw.job) return;
    W.sw.job = j; changed();
    wait(siteId(j.app), null, j).then(err => finish(j, err));
  }
  async function wait(site, loc, job) {
    const started = new Date(job.started_at).getTime(), end = started + TIMEOUT_MS;
    while (Date.now() < end) {
      await sleep(POLL_MS);
      let t;
      try { t = await token(false); } catch { continue; }   // 매번 받는다(대부분 캐시). 만료돼도 기다리는 중엔 화면을 떠나지 않음
      if (loc) {
        try {
          const res = await arm(t, "GET", loc.url);
          if (res.status === 202) continue;
          const j = await res.json().catch(() => ({}));
          if (!res.ok) return j.error?.message || `HTTP ${res.status}`;
          if (loc.async || j.status) {   // Azure-AsyncOperation 은 200 + {status} 로 진행 상태를 준다
            const st = String(j.status || "").toLowerCase();
            if (st === "succeeded") return null;
            if (st === "failed" || st === "canceled" || st === "cancelled") return j.error?.message || `작업 ${j.status}`;
            continue;
          }
          return null;
        } catch { loc = null; }   // 작업 주소를 못 읽으면(CORS 등) 사이트의 마지막 swap 시각으로 판정
      } else {
        const s = await swapStamp(t, site);
        // 시작 전 시각을 못 읽었으면 이번 swap 시작 뒤에 찍힌 시각만 완료로 본다(예전 swap 을 완료로 오판하지 않게)
        if (s && s !== job.before && (job.before || new Date(s).getTime() >= started - 30000)) return null;
      }
    }
    return `${TIMEOUT_MS / 60000}분 안에 끝나지 않음 — 포털·활동 기록에서 상태 확인`;
  }
  async function finish(job, err) {
    const done = {...job, ok: !err, error: err, finished_at: now()};
    let stamp = null;
    if (!err) { try { stamp = await swapStamp(await token(false), siteId(job.app)); } catch {} }   // 되돌리기 전 「그 사이 다른 swap」 확인용
    loadUndo();   // 다른 탭이 바꾼 것과 합친다
    if (!err && job.action === "swap") {
      W.sw.undo[job.repo] = {at: done.finished_at, until: new Date(Date.now() + UNDO_MIN * 60000).toISOString(), id: job.id, stamp};
    } else if (!err && job.action === "undo") delete W.sw.undo[job.repo];
    if (!err) W.sw.pending[job.repo] = {started_at: job.started_at, finished_at: done.finished_at, action: job.action};   // 보드 반영 전 다시 swap 막기
    saveUndo();
    setLS(key("webJob"), null);
    W.sw.last = done; W.sw.job = null;
    const what = job.action === "undo" ? "되돌리기" : "swap";
    W.toast(err ? `${job.app} ${what} 실패\n${err}` : `${job.app} ${what} 완료 — 보드에는 다음 수집에 반영됩니다(수집 PC 가 2분 안에 감지)`, !!err);
    changed();
  }

  return Object.assign(W, {actionMatch, swapAllowed, init, login, logout, checkPerms, start, undoOpen,
                           armUrl: app => W.azure ? `${ARM}${siteId(app)}/slots/staging/slotsswap` : ""});
})();
if (typeof module !== "undefined") module.exports = WEBLOGIN;
