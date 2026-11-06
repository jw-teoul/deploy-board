"""배포 현황 보드 로컬 서버 — web/ 정적 파일과 data/state.json 을 서빙하고, 수집기를 백그라운드로 돌린다.

실행: python server.py [--port 8765] [--bind 0.0.0.0] [--no-collect] [--no-watch] [--check 30] [--swap-check 60]   → http://localhost:8765
`POST /api/refresh` 에 인증이 없으므로 공용 망에서는 `--bind 127.0.0.1` 로 묶는다(README 「주의」).

실행 방법 세 가지(코드 하나, README 「실행 방법」):
- 기본: 수집·새로고침, swap 없음(읽기만).
- 공유용 `--readonly`: 수집·감시·새로고침 없이 data/state.json 만 보여 준다. 수집 PC 가 `--publish` 로 올린 파일을 쓴다.
- 내 PC 용 `--allow-swap`: 127.0.0.1/localhost 바인딩일 때만 켜지고 `--readonly` 와 함께 쓸 수 없다. swap.py 참고.

변경 감시(기본 켜짐): `--check` 초마다 레포별로 가벼운 조회(원격 브랜치 끝 커밋 ls-remote 1회 + 최근 PR 갱신 시각 1회)를 하고,
`--swap-check` 초마다 리소스 그룹 사이트의 마지막 swap(slotSwapStatus, az 1회)을 본다. 바뀐 레포만 다시 수집해 state.json 에 합친다. 활동 기록에 아직 없는 swap 은 잠정(pending_swap)으로 운영 SHA 를 추정하고, 활동 기록에 확정이 올라오면 다시 수집해 푼다.
웹훅(GitHub push/PR, Azure Event Grid)은 외부에서 접근되는 주소가 필요해 지금은 쓸 수 없다 — 나중에 붙일 때는
`POST /api/refresh {"repos":["adminsite"]}` 를 호출하면 된다.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import collector
import notice
import publish
import slack
import swap

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
STATE = os.path.join(HERE, "data", "state.json")
SWAP_LOG = os.path.join(HERE, "data", "swap-log.jsonl")
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# 실행 방법(main 에서 정함). readonly 와 swap 은 함께 켜질 수 없다.
OPT = {"readonly": False, "swap": None, "publish": None, "token": None, "port": None, "slack": None}

# ---------- 정적 파일 버전(캐시 무효화) ----------
# index.html 을 내보낼 때 pixel.js 주소에 내용 해시(?v=)를 붙이고, 그림 파일 묶음의 해시를 data-assets 로 넘긴다.
# 버전이 붙은 요청은 오래 캐시하고, index.html 과 버전 없는 요청은 매번 서버에 확인(no-cache)하게 한다.
_ver_cache = {}


def file_version(path):
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    if key not in _ver_cache:
        with open(path, "rb") as f:
            _ver_cache[key] = hashlib.sha1(f.read()).hexdigest()[:8]
    return _ver_cache[key]


def dir_version(root):
    h = hashlib.sha1()
    for base, dirs, files in sorted(os.walk(root)):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(base, name)
            h.update(os.path.relpath(full, root).replace(os.sep, "/").encode())
            h.update(file_version(full).encode())
    return h.hexdigest()[:8]


def render_index():
    with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
        html = f.read()
    js = file_version(os.path.join(WEB, "pixel.js"))
    assets = dir_version(os.path.join(WEB, "assets"))
    html = html.replace('<script src="pixel.js"></script>',
                        f'<script src="pixel.js?v={js}" data-assets="{assets}"></script>', 1)
    if OPT["token"]:  # swap 요청 위조 방어용 토큰 — 이 서버가 내보낸 화면만 안다
        html = html.replace("</head>", f'<meta name="board-token" content="{OPT["token"]}">\n</head>', 1)
    wl = file_version(os.path.join(WEB, "weblogin.js"))
    html = html.replace('<script src="weblogin.js"></script>', f'<script src="weblogin.js?v={wl}"></script>', 1)
    return html


def csp(html):
    """화면의 Content-Security-Policy. 웹 로그인 토큰이 이 화면에 있으므로 스크립트는 이 서버 파일과 화면 안 인라인(해시 일치)만,
    접속은 이 서버·Microsoft 로그인·Azure Resource Manager 만 허용한다."""
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(js.encode("utf-8")).digest()).decode()
                      for js in re.findall(r"<script>(.*?)</script>", html, re.S))
    return ("default-src 'self'; "
            f"script-src 'self' {hashes}; "
            "connect-src 'self' https://login.microsoftonline.com https://management.azure.com; "
            "frame-src https://login.microsoftonline.com; "
            "img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'self'")   # 'self': 로그인 갱신(숨은 iframe)이 로그인 응답을 이 화면 주소로 iframe 안에 연다


def web_login(cfg):
    """웹 로그인 swap 설정(공개값). swap 모드(서버 az 로 swap)에서는 서버 쪽 swap 을 쓰므로 주지 않는다."""
    e = cfg.get("entra") or {}
    if status["mode"] == "swap" or not e.get("tenant_id") or not e.get("client_id"):
        return None
    return {"tenant_id": e["tenant_id"], "client_id": e["client_id"],
            "msal": "vendor/msal-browser.min.js?v=" + file_version(os.path.join(WEB, "vendor", "msal-browser.min.js"))}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


status = {
    "running": False, "running_repos": None, "started_at": None, "finished_at": None, "last_error": None, "runs": 0,
    "collected_at": None,
    "watch": {"enabled": False, "check_s": 0, "swap_check_s": 0, "last_check": None, "last_swap_check": None,
              "changes": {}, "probe_error": None, "checks": 0},
    "mode": "local", "swap": {"enabled": False}, "publish": None,
    "accounts": None,   # 이 PC 의 az·gh 로그인(loopback 바인딩일 때만 채움 — 남에게 계정 이름을 보이지 않게)
}
lock = threading.Lock()
if os.path.exists(STATE):
    try:
        with open(STATE, encoding="utf-8") as f:
            status["collected_at"] = json.load(f).get("collected_at")
    except Exception:
        pass


def refresh(only=None):
    """수집 실행(동기). only 가 있으면 그 레포만 수집해 기존 state 에 합친다.
    돌려주는 값: 이미 수집 중이라 시작 못 했으면 None, 아니면 실패한 레포 이름 목록(예외면 대상 전부, 결과에 errors 가 있는 레포)."""
    with lock:
        if status["running"]:
            return None
        status.update(running=True, running_repos=only, started_at=now(), last_error=None)
    failed = []
    try:
        state = collector.Collector(collector.load_cfg()).collect(only)
        failed = [r["name"] for r in state.get("repos", []) if r.get("errors")]
        if only:
            state = collector.merge_state(state, STATE)
        collector.write_state(state, STATE)
        status["collected_at"] = state["collected_at"]
        do_publish()
    except Exception:
        status["last_error"] = traceback.format_exc()[-800:]
        failed = list(only) if only else [rc["name"] for rc in (_safe_cfg().get("repos") or [])]
    finally:
        with lock:
            status.update(running=False, running_repos=None, finished_at=now())
            status["runs"] += 1
    return failed


def _safe_cfg():
    try:
        return collector.load_cfg()
    except Exception:
        return {}


publish_lock = threading.Lock()


def do_publish():
    """--publish 대상이 있으면 방금 쓴 state.json 을 올린다(실패는 화면 꼬리표로, 다시 올리기는 PublishRetry)."""
    if not OPT["publish"]:
        return
    with publish_lock:   # 수집 뒤 올리기와 재시도가 겹쳐 같은 파일을 두 번 동시에 올리지 않게
        err = publish.publish(OPT["publish"], STATE)
        status["publish"].update(last=now(), error=err)
        if not err:
            status["publish"]["ok_at"] = status["publish"]["last"]


class PublishRetry(threading.Thread):
    """마지막 올리기가 실패했으면 1분 뒤부터 다시 올린다. 실패가 이어지면 2분·4분·… 최대 5분 간격. 수집 중이면 미룬다
    (수집이 끝나면 어차피 올린다)."""

    def __init__(self, tick_s=15, first_s=60, max_s=300):
        super().__init__(daemon=True)
        self.tick_s, self.first_s, self.max_s = tick_s, first_s, max_s
        self.fails, self.next_at = 0, None

    def step(self, t):
        p = status.get("publish") or {}
        if not p.get("error"):
            self.fails, self.next_at = 0, None
            return False
        if self.next_at is None:
            self.next_at = t + min(self.first_s * 2 ** self.fails, self.max_s)
        if t < self.next_at or status["running"]:
            return False
        do_publish()
        self.fails = self.fails + 1 if (status.get("publish") or {}).get("error") else 0
        self.next_at = None
        return True

    def run(self):
        while True:
            time.sleep(self.tick_s)
            try:
                self.step(time.time())
            except Exception:   # 다음 차례에 다시. 화면 꼬리표로 남긴다
                if status.get("publish") is not None:
                    status["publish"]["error"] = "재시도 오류: " + traceback.format_exc()[-300:]


_state_mtime = [None]


def sync_collected_at():
    """공유용: 수집 PC 가 올린 state.json 이 바뀌었으면 collected_at 을 다시 읽는다(화면이 이걸 보고 다시 그린다)."""
    try:
        m = os.stat(STATE).st_mtime_ns
    except OSError:
        return
    if m != _state_mtime[0]:
        try:
            with open(STATE, encoding="utf-8") as f:
                status["collected_at"] = json.load(f).get("collected_at")
            _state_mtime[0] = m
        except Exception:
            pass   # 바꿔 끼우는 순간이면 다음에 다시


def slack_result(done):
    """swap·되돌리기가 끝나면 예고 글의 스레드에 결과를 단다(예고를 보낸 swap 만). → swap-log 의 slack 칸."""
    th = (done.get("info") or {}).get("slack")
    if not th or not OPT["slack"]:
        return None
    text, why = notice.result_text(done)
    if not text:
        return dict(th, result_ok=False, result_error=why)
    ok, _, err = slack.post(OPT["slack"], text, thread_ts=th["ts"])
    return dict(th, result_ok=ok, result_error=err)


def slack_preview(repo, action, info, req):
    """swap 요청에 Slack 예고가 있으면 swap 전에 보낸다 → (오류 문구 또는 None). 보내면 info["slack"] 에 스레드를 적는다.
    되돌리기는 예고 없이 원래 swap 의 스레드를 이어 쓴다."""
    if action == "undo":
        u = OPT["swap"].status["undo"].get(repo) or {}
        if u.get("slack") and OPT["slack"]:
            info["slack"] = dict(u["slack"])
        return None
    if not req:
        return None
    if not OPT["slack"]:
        return "Slack 이 꺼져 있음(환경 변수 없음) — 체크를 끄고 다시 누름"
    text = (req.get("text") or "").strip() if isinstance(req, dict) else ""
    if not text:
        return "Slack 예고 문구가 비어 있음"
    if len(slack.escape(text)) > slack.MAX_TEXT:
        return f"Slack 예고 문구가 {slack.MAX_TEXT}자를 넘음"
    ok, why, _, _ = OPT["swap"].can_start(repo, action)
    if not ok:   # 시작 못 할 swap 의 예고를 보내지 않는다
        return why
    sent, ts, err = slack.post(OPT["slack"], text)
    if not sent:
        return f"Slack 예고 전송 실패 — swap 하지 않음({err}). 체크를 끄면 swap 만 함"
    info["slack"] = {"channel": OPT["slack"]["channel"], "ts": ts}
    return None


def after_swap(repo):
    """swap 이 끝나면 그 레포만 다시 수집한다(수집 중이면 끝날 때까지 기다림)."""
    while status["running"]:
        time.sleep(1)
    refresh([repo])


def start_refresh(only=None):
    if status["running"]:
        return False
    threading.Thread(target=refresh, args=(only,), daemon=True).start()
    return True


def load_state():
    if not os.path.exists(STATE):
        return None
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def repo_state(name):
    return next((r for r in (load_state() or {}).get("repos", []) if r.get("name") == name), None)


def check_accounts():
    status["accounts"] = swap.whoami()


def account_loop(every_s=3600):
    while True:
        try:
            check_accounts()
        except Exception:
            traceback.print_exc()
        time.sleep(every_s)


def notice_for(name):
    """배포 알림 문구 초안 — 예고(지금 스테이징에만 있는 것)와 결과(이 PC 의 마지막 swap 기록)."""
    r = repo_state(name)
    if not r:
        return {"error": "없는 레포"}
    snap = notice.snapshot(r)
    pre, pre_why = notice.preview_text(snap)
    rec = None if OPT["readonly"] else notice.last_record(SWAP_LOG, name)
    res, res_why = notice.result_text(rec)
    if OPT["readonly"] and not res:
        res_why = "공유 서버에는 swap 기록이 없음 — swap 한 PC 의 보드에서 확인"
    return {"repo": name, "label": snap["label"], "collected_at": (load_state() or {}).get("collected_at"),
            "preview": pre, "preview_why": pre_why, "result": res, "result_why": res_why,
            "result_at": (rec or {}).get("finished_at")}


class Watcher(threading.Thread):
    """가벼운 조회로 변경을 감지해 바뀐 레포만 재수집한다."""

    def __init__(self, check_s, swap_s):
        super().__init__(daemon=True)
        self.check_s, self.swap_s = check_s, swap_s
        self.fp = {}        # repo -> {"refs","pr"}
        self.swap = {}      # repo -> 마지막 swap 시각
        self.last_swap_check = 0
        self.confirm_at = {}  # repo -> 마지막 확정 확인 시각(time.time)
        self.retry = {}       # repo -> (이유, 다시 수집할 시각, 실패 횟수) — 감지한 변경의 수집이 실패한 레포
        self.retry_s, self.retry_max_s = 300, 3600   # 첫 재시도 5분, 실패가 이어지면 두 배씩 최대 1시간
        status["watch"].update(enabled=True, check_s=check_s, swap_check_s=swap_s)

    def run(self):
        time.sleep(5)
        while True:
            try:
                self.tick()
            except Exception:
                status["watch"]["probe_error"] = traceback.format_exc()[-400:]
            time.sleep(self.check_s)

    @staticmethod
    def confirm_due(p, last_try=0, fast_s=15 * 60, slow_every_s=10 * 60, give_up_s=6 * 3600):
        """잠정 swap 의 확정 확인을 할지: 생긴 지 fast_s 안이면 swap 확인마다, 그 뒤 give_up_s 까지는 slow_every_s 마다.
        그 뒤에는 「잠정」 표시로 남긴다(다음 수집 때 다시 판정)."""
        try:
            age = (dt.datetime.now(dt.timezone.utc) - collector.iso(p["time"])).total_seconds()
        except Exception:   # 잠정 없음·시각 이상
            return False
        if age <= fast_s:
            return True
        return age <= give_up_s and time.time() - last_try >= slow_every_s

    @staticmethod
    def missed_at_start(site_entry, r):
        """첫 확인 때: 저장된 state 가 모르는 swap(잠정으로도 없음)이 사이트에 있으면 서버가 꺼져 있던 동안의 swap — 다시 수집."""
        if not site_entry or not r or "swaps" not in r:
            return False
        if r.get("pending_swap", {}).get("time") == site_entry.get("time"):
            return False
        try:
            return collector.Collector.pending_swap(site_entry, r.get("swaps") or []) is not None
        except Exception:
            return False

    def tick(self):
        cfg = collector.load_cfg()
        c = collector.Collector(cfg)
        changed = []
        do_swap = time.time() - self.last_swap_check >= self.swap_s
        site, swap_err, pend = None, None, {}
        if do_swap:
            # 사이트 slotSwapStatus 를 리소스 그룹 1회로 본다 — 활동 기록(앱마다 1회, 3~5분 늦음)보다 빠르고 가볍다
            try:
                site = c.site_swaps()
            except Exception as e:
                swap_err = f"사이트 swap 상태 조회 실패 — {e}"
            st_repos = {r["name"]: r for r in (load_state() or {}).get("repos", [])}
            pend = {n: r["pending_swap"] for n, r in st_repos.items() if r.get("pending_swap")}
        for rc in cfg["repos"]:
            name = rc["name"]
            fp = c.probe_repo(rc)
            if name in self.fp and fp != self.fp[name]:
                changed.append((name, "refs" if fp["refs"] != self.fp[name]["refs"] else "pr"))
            self.fp[name] = fp
            if site is not None and rc.get("app"):
                sw = (site.get(rc["app"]) or {}).get("time")
                if name in self.swap and sw != self.swap[name]:
                    changed.append((name, "swap"))
                elif name not in self.swap and self.missed_at_start(site.get(rc["app"]), st_repos.get(name)):
                    changed.append((name, "swap(시작 보정)"))
                self.swap[name] = sw
            if do_swap and rc.get("app") and self.confirm_due(pend.get(name), self.confirm_at.get(name, 0)):
                # 잠정 swap 이 활동 기록에 확정으로 올라왔으면 다시 수집해 잠정을 푼다
                self.confirm_at[name] = time.time()
                try:
                    a = c.probe_swap(rc["app"])
                    if a and (collector.iso(a) - collector.iso(pend[name]["time"])).total_seconds() >= -collector.SAME_SWAP_S:
                        changed.append((name, "swap 확정"))
                except Exception as e:   # 이 앱 확인만 실패 — 이미 감지한 다른 변경은 그대로 수집한다
                    swap_err = f"{name} swap 확정 확인 실패 — {e}"
        for name, (why, at, _) in list(self.retry.items()):
            if time.time() >= at and name not in {n for n, _ in changed}:
                changed.append((name, f"재시도({why})"))
        if do_swap:
            self.last_swap_check = time.time()
            status["watch"]["last_swap_check"] = now()
        status["watch"]["last_check"] = now()
        status["watch"]["checks"] += 1
        status["watch"]["probe_error"] = swap_err
        if changed:
            for name, why in changed:
                status["watch"]["changes"][name] = {"at": now(), "why": why}
            names = sorted({n for n, _ in changed})
            while status["running"]:   # 수동 새로고침 중이면 끝날 때까지 기다린다
                time.sleep(1)
            failed = refresh(names)
            if failed is None:         # 그사이 다른 수집이 먼저 시작함 — 전부 나중에 다시
                failed = names
            whys = {n: w for n, w in changed if not w.startswith("재시도(")}
            for name in names:   # 지문은 이미 새 값이라 다시 감지되지 않는다 → 실패한 레포는 따로 기억해 다시 수집
                if name in failed:
                    why0, _, n = self.retry.get(name, ("", 0, 0))
                    wait = min(self.retry_s * 2 ** n, self.retry_max_s)
                    self.retry[name] = (whys.get(name) or why0, time.time() + wait, n + 1)
                else:
                    self.retry.pop(name, None)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, fmt, *args):  # 조용히
        pass

    def end_headers(self):
        # 정적 파일 응답에 캐시 지시를 붙인다(_json 은 직접 no-store 를 보냄).
        if not getattr(self, "_cache_set", False):
            versioned = "v=" in (self.path.split("?", 1)[1] if "?" in self.path else "")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable" if versioned else "no-cache")
        self._cache_set = False   # keep-alive 로 같은 핸들러가 다음 요청을 받을 때를 위해 되돌린다
        # 화면(_index) 말고 모든 응답에도 가장 좁은 CSP 를 붙인다 — index.html 을 다른 경로로 열거나 다른 html 이 생겨도 스크립트가 돌지 않게
        if not getattr(self, "_csp_set", False):
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
        self._csp_set = False
        super().end_headers()

    def list_directory(self, path):   # 폴더 목록(html)은 내주지 않는다
        self.send_error(404)
        return None

    def _is_index(self, path):
        """/, /index.html 과 같은 파일로 풀리는 모든 경로(/./index.html, /Index.html(Windows), %2e 등)를 화면으로 본다."""
        t = os.path.normcase(os.path.realpath(self.translate_path(path)))
        web = os.path.normcase(os.path.realpath(WEB))
        return t in (web, os.path.join(web, "index.html"))

    def _index(self):
        html = render_index()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", csp(html))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self._csp_set = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._cache_set = True
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            if not os.path.exists(STATE):
                return self._json({"repos": [], "collected_at": None, "errors": ["아직 수집 전"], "status": status})
            with open(STATE, encoding="utf-8") as f:
                data = json.load(f)
            data["status"] = status
            data["entra"] = web_login(collector.load_cfg())
            return self._json(data)
        if path == "/api/status":
            if OPT["readonly"]:
                sync_collected_at()
            return self._json(status)
        if path == "/api/notice":
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            out = notice_for((q.get("repo") or [""])[0])
            return self._json(out, 404 if "error" in out else 200)
        if self._is_index(path):
            return self._index()
        return super().do_GET()

    def do_HEAD(self):
        if self._is_index(self.path.split("?", 1)[0]):
            return self._index()
        return super().do_HEAD()

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def _swap_guard(self):
        """swap 요청은 이 PC 의 이 화면에서 온 것만 받는다: 접속 주소 loopback, Host·Origin 이 127.0.0.1/localhost:포트, 토큰 일치."""
        hosts = {f"{h}:{OPT['port']}" for h in ("127.0.0.1", "localhost", "[::1]")}
        origin = self.headers.get("Origin")
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return "이 PC 에서 온 요청만 받음"
        if self.headers.get("Host", "") not in hosts or (origin and origin not in {"http://" + h for h in hosts}):
            return "Host·Origin 불일치"
        if not secrets.compare_digest(self.headers.get("X-Board-Token", ""), OPT["token"] or ""):
            return "토큰 불일치(화면을 새로고침)"
        return None

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/swap":
            if not OPT["swap"]:
                return self._json({"error": "swap 이 꺼진 서버(--allow-swap 으로 이 PC 에서만 켬)"}, 403)
            bad = self._swap_guard()
            if bad:
                return self._json({"error": bad}, 403)
            body = self._body()
            info = {"client": body.get("info") or {}}
            if body.get("action") == "swap":   # 나간 것의 목록은 화면 값이 아니라 서버의 state.json 으로 남긴다
                r = repo_state(body.get("repo"))
                if r:
                    info["snapshot"] = notice.snapshot(r)
            if body.get("action") == "undo":   # 되돌리기 결과 문구의 이름(레포 label)
                r = repo_state(body.get("repo"))
                if r:
                    info["snapshot"] = {"label": r.get("label") or r.get("name")}
            err = slack_preview(body.get("repo"), body.get("action"), info, body.get("slack"))
            if err:
                return self._json({"started": False, "reason": err, "status": status}, 409)
            ok, why = OPT["swap"].start(body.get("repo"), body.get("action"), info)
            if not ok and info.get("slack") and body.get("action") == "swap":   # 예고 뒤에 시작 못 하면 스레드에 알린다
                sent, _, serr = slack.post(OPT["slack"], f"swap 시작 못 함 — 운영은 그대로\n이유: {why}", thread_ts=info["slack"]["ts"])
                why = f"{why} — Slack 예고는 이미 보냄, " + ("스레드에 「시작 못 함」을 달았음" if sent else f"스레드 알림도 실패({serr})")
                return self._json({"started": False, "reason": why, "slack_sent": True, "status": status}, 409)
            return self._json({"started": ok, "reason": why, "status": status}, 202 if ok else 409)
        if path == "/api/recheck":
            if not OPT["swap"]:
                return self._json({"error": "swap 이 꺼진 서버"}, 403)
            bad = self._swap_guard()
            if bad:
                return self._json({"error": bad}, 403)
            if not OPT["swap"].begin_check():   # 「권한 다시 확인」: 로그인 상태와 swap 권한을 다시 조회(백그라운드)
                return self._json({"started": False, "reason": "이미 확인 중", "status": status}, 409)
            threading.Thread(target=OPT["swap"].run_check, args=(check_accounts,), daemon=True).start()
            return self._json({"started": True, "status": status}, 202)
        if path == "/api/refresh" and OPT["readonly"]:
            return self._json({"error": "보기 전용 서버 — 수집은 수집 PC 에서 한다"}, 403)
        if path == "/api/refresh":
            started = start_refresh(self._body().get("repos"))
            return self._json({"started": started, "status": status}, 202 if started else 409)
        self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="0.0.0.0", help="수신 주소. 혼자 볼 때는 127.0.0.1 (refresh 엔드포인트에 인증이 없다)")
    ap.add_argument("--no-collect", action="store_true", help="시작할 때 전체 수집하지 않음")
    ap.add_argument("--no-watch", action="store_true", help="변경 감시 끄기")
    ap.add_argument("--check", type=int, default=30, help="레포 변경 확인 간격(초)")
    ap.add_argument("--swap-check", type=int, default=60, help="Azure swap 확인 간격(초) — 사이트 상태 1회 조회")
    ap.add_argument("--open", action="store_true", help="브라우저 열기")
    ap.add_argument("--readonly", action="store_true", help="공유용 보기 전용: 수집·감시·새로고침 없이 data/state.json 만 보여 줌")
    ap.add_argument("--allow-swap", action="store_true", help="내 PC 용: swap·되돌리기 버튼 켜기(127.0.0.1/localhost 바인딩일 때만)")
    ap.add_argument("--publish", metavar="대상", help="수집할 때마다 state.json 을 올릴 곳(예 datacrawler:deploy-board/data/state.json)")
    a = ap.parse_args()
    if a.readonly and (a.allow_swap or a.publish):
        sys.exit("--readonly 는 --allow-swap·--publish 와 함께 쓸 수 없다(공유 서버에서는 swap·수집을 하지 않는다)")
    if a.allow_swap and a.bind not in LOOPBACK:
        sys.exit("--allow-swap 은 이 PC 에서만 쓴다: --bind 127.0.0.1 을 함께 준다")
    OPT["port"] = a.port
    if a.readonly:
        OPT["readonly"] = True
        status["mode"] = "readonly"
        a.no_collect = a.no_watch = True
        sync_collected_at()
    if a.publish:
        OPT["publish"] = a.publish
        status["publish"] = {"target": a.publish, "last": None, "ok_at": None, "error": None}
        threading.Thread(target=do_publish, daemon=True).start()   # 지금 가진 state 부터 한 번 올린다
        PublishRetry().start()
    if a.allow_swap:
        OPT["token"] = secrets.token_urlsafe(24)
        global SWAP_LOG
        if os.environ.get("DEPLOY_BOARD_SWAP_CMD"):   # 가짜 명령 시험 기록은 실제 swap 기록과 섞지 않는다(알림 결과 문구가 읽음)
            SWAP_LOG = SWAP_LOG.replace(".jsonl", ".fake.jsonl")
        OPT["slack"], why = slack.config()
        for k in ("DEPLOY_BOARD_SLACK_TOKEN", "DEPLOY_BOARD_SLACK_CHANNEL", "DEPLOY_BOARD_SLACK_API"):
            os.environ.pop(k, None)   # az·gh·git 같은 자식 프로세스가 봇 토큰을 물려받지 않게
        status["slack"] = slack.public(OPT["slack"], why)
        OPT["swap"] = swap.SwapRunner(collector.load_cfg, SWAP_LOG, on_done=after_swap, notify=slack_result)
        status["mode"] = "swap"
        status["swap"] = OPT["swap"].status
        threading.Thread(target=OPT["swap"].perm_loop, daemon=True).start()
    if not a.readonly and a.bind in LOOPBACK:
        threading.Thread(target=account_loop, daemon=True).start()
    if not a.no_collect:
        start_refresh()
    if not a.no_watch:
        Watcher(a.check, a.swap_check).start()
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    mode = {"readonly": "공유용 보기 전용", "swap": "내 PC 용 swap 켜짐", "local": "기본(읽기만)"}[status["mode"]]
    print(f"배포 현황 보드 [{mode}]: http://localhost:{a.port}  (수신 {a.bind}, 감시 {'꺼짐' if a.no_watch else f'{a.check}s / swap {a.swap_check}s'}, Ctrl+C 로 종료)")
    if a.open:
        webbrowser.open(f"http://localhost:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
