"""배포 현황 보드 로컬 서버 — web/ 정적 파일과 data/state.json 을 서빙하고, 수집기를 백그라운드로 돌린다.

실행: python server.py [--port 8765] [--bind 0.0.0.0] [--no-collect] [--no-watch] [--check 30] [--swap-check 120]   → http://localhost:8765
`POST /api/refresh` 에 인증이 없으므로 공용 망에서는 `--bind 127.0.0.1` 로 묶는다(README 「주의」).

변경 감시(기본 켜짐): `--check` 초마다 레포별로 가벼운 조회(원격 브랜치 끝 커밋 ls-remote 1회 + 최근 PR 갱신 시각 1회)를 하고,
`--swap-check` 초마다 Azure 활동 기록에서 마지막 swap 시각을 본다. 바뀐 레포만 다시 수집해 state.json 에 합친다.
웹훅(GitHub push/PR, Azure Event Grid)은 외부에서 접근되는 주소가 필요해 지금은 쓸 수 없다 — 나중에 붙일 때는
`POST /api/refresh {"repos":["adminsite"]}` 를 호출하면 된다.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import threading
import time
import traceback
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import collector

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
STATE = os.path.join(HERE, "data", "state.json")

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
    return html.replace('<script src="pixel.js"></script>',
                        f'<script src="pixel.js?v={js}" data-assets="{assets}"></script>', 1)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


status = {
    "running": False, "running_repos": None, "started_at": None, "finished_at": None, "last_error": None, "runs": 0,
    "collected_at": None,
    "watch": {"enabled": False, "check_s": 0, "swap_check_s": 0, "last_check": None, "last_swap_check": None,
              "changes": {}, "probe_error": None, "checks": 0},
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
    except Exception:
        status["last_error"] = traceback.format_exc()[-800:]
    finally:
        with lock:
            status.update(running=False, running_repos=None, finished_at=now())
            status["runs"] += 1
    return True


def start_refresh(only=None):
    if status["running"]:
        return False
    threading.Thread(target=refresh, args=(only,), daemon=True).start()
    return True


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
        super().end_headers()

    def _index(self):
        body = render_index().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
            return self._json(data)
        if path == "/api/status":
            return self._json(status)
        if path in ("/", "/index.html"):
            return self._index()
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/refresh":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            started = start_refresh(body.get("repos"))
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
    a = ap.parse_args()
    if not a.no_collect:
        start_refresh()
    if not a.no_watch:
        Watcher(a.check, a.swap_check).start()
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print(f"배포 현황 보드: http://localhost:{a.port}  (수신 {a.bind}, 감시 {'꺼짐' if a.no_watch else f'{a.check}s / swap {a.swap_check}s'}, Ctrl+C 로 종료)")
    if a.open:
        webbrowser.open(f"http://localhost:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
