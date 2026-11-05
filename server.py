"""배포 현황 보드 로컬 서버 — web/ 정적 파일과 data/state.json 을 서빙하고, 수집기를 백그라운드로 돌린다.

실행: python server.py [--port 8765] [--bind 0.0.0.0] [--no-collect] [--no-watch] [--check 30] [--swap-check 120]   → http://localhost:8765
`POST /api/refresh` 에 인증이 없으므로 공용 망에서는 `--bind 127.0.0.1` 로 묶는다(README 「주의」).

실행 방법 세 가지(코드 하나, README 「실행 방법」):
- 기본: 수집·새로고침, swap 없음(읽기만).
- 공유용 `--readonly`: 수집·감시·새로고침 없이 data/state.json 만 보여 준다. 수집 PC 가 `--publish` 로 올린 파일을 쓴다.
- 내 PC 용 `--allow-swap`: 127.0.0.1/localhost 바인딩일 때만 켜지고 `--readonly` 와 함께 쓸 수 없다. swap.py 참고.

변경 감시(기본 켜짐): `--check` 초마다 레포별로 가벼운 조회(원격 브랜치 끝 커밋 ls-remote 1회 + 최근 PR 갱신 시각 1회)를 하고,
`--swap-check` 초마다 Azure 활동 기록에서 마지막 swap 시각을 본다. 바뀐 레포만 다시 수집해 state.json 에 합친다.
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
import swap

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
STATE = os.path.join(HERE, "data", "state.json")
SWAP_LOG = os.path.join(HERE, "data", "swap-log.jsonl")
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# 실행 방법(main 에서 정함). readonly 와 swap 은 함께 켜질 수 없다.
OPT = {"readonly": False, "swap": None, "publish": None, "token": None, "port": None}

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
    """수집 실행(동기). only 가 있으면 그 레포만 수집해 기존 state 에 합친다."""
    with lock:
        if status["running"]:
            return False
        status.update(running=True, running_repos=only, started_at=now(), last_error=None)
    try:
        state = collector.Collector(collector.load_cfg()).collect(only)
        if only:
            state = collector.merge_state(state, STATE)
        collector.write_state(state, STATE)
        status["collected_at"] = state["collected_at"]
        do_publish()
    except Exception:
        status["last_error"] = traceback.format_exc()[-800:]
    finally:
        with lock:
            status.update(running=False, running_repos=None, finished_at=now())
            status["runs"] += 1
    return True


def do_publish():
    """--publish 대상이 있으면 방금 쓴 state.json 을 올린다(실패는 화면 꼬리표로)."""
    if not OPT["publish"]:
        return
    err = publish.publish(OPT["publish"], STATE)
    status["publish"].update(last=now(), error=err)
    if not err:
        status["publish"]["ok_at"] = status["publish"]["last"]


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
        status["watch"].update(enabled=True, check_s=check_s, swap_check_s=swap_s)

    def run(self):
        time.sleep(5)
        while True:
            try:
                self.tick()
            except Exception:
                status["watch"]["probe_error"] = traceback.format_exc()[-400:]
            time.sleep(self.check_s)

    def tick(self):
        cfg = collector.load_cfg()
        c = collector.Collector(cfg)
        changed = []
        do_swap = time.time() - self.last_swap_check >= self.swap_s
        for rc in cfg["repos"]:
            name = rc["name"]
            fp = c.probe_repo(rc)
            if name in self.fp and fp != self.fp[name]:
                changed.append((name, "refs" if fp["refs"] != self.fp[name]["refs"] else "pr"))
            self.fp[name] = fp
            if do_swap and rc.get("app"):
                sw = c.probe_swap(rc["app"])
                if name in self.swap and sw != self.swap[name]:
                    changed.append((name, "swap"))
                self.swap[name] = sw
        if do_swap:
            self.last_swap_check = time.time()
            status["watch"]["last_swap_check"] = now()
        status["watch"]["last_check"] = now()
        status["watch"]["checks"] += 1
        status["watch"]["probe_error"] = None
        if changed:
            for name, why in changed:
                status["watch"]["changes"][name] = {"at": now(), "why": why}
            names = sorted({n for n, _ in changed})
            while status["running"]:   # 수동 새로고침 중이면 끝날 때까지 기다린다
                time.sleep(1)
            refresh(names)


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
            ok, why = OPT["swap"].start(body.get("repo"), body.get("action"), info)
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
    ap.add_argument("--swap-check", type=int, default=120, help="Azure swap 확인 간격(초)")
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
    if a.allow_swap:
        OPT["token"] = secrets.token_urlsafe(24)
        global SWAP_LOG
        if os.environ.get("DEPLOY_BOARD_SWAP_CMD"):   # 가짜 명령 시험 기록은 실제 swap 기록과 섞지 않는다(알림 결과 문구가 읽음)
            SWAP_LOG = SWAP_LOG.replace(".jsonl", ".fake.jsonl")
        OPT["swap"] = swap.SwapRunner(collector.load_cfg, SWAP_LOG, on_done=after_swap)
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
