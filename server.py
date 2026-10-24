"""배포 현황 보드 로컬 서버 — web/ 정적 파일과 data/state.json 을 서빙하고, POST /api/refresh 로 수집기를 백그라운드로 돌린다.

실행: python server.py [--port 8765] [--no-collect]   → http://localhost:8765
"""
import argparse
import datetime as dt
import json
import os
import threading
import traceback
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import collector

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
STATE = os.path.join(HERE, "data", "state.json")

status = {"running": False, "started_at": None, "finished_at": None, "last_error": None, "runs": 0}
lock = threading.Lock()


def refresh(only=None):
    with lock:
        if status["running"]:
            return False
        status.update(running=True, started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), last_error=None)
    try:
        state = collector.Collector(collector.load_cfg()).collect(only)
        collector.write_state(state, STATE)
    except Exception:
        status["last_error"] = traceback.format_exc()[-800:]
    finally:
        with lock:
            status.update(running=False, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
            status["runs"] += 1
    return True


def start_refresh(only=None):
    if status["running"]:
        return False
    threading.Thread(target=refresh, args=(only,), daemon=True).start()
    return True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, fmt, *args):  # 조용히
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            if not os.path.exists(STATE):
                return self._json({"repos": [], "collected_at": None, "errors": ["아직 수집 전"], "status": status})
            with open(STATE, encoding="utf-8") as f:
                body = f.read()
            data = json.loads(body)
            data["status"] = status
            return self._json(data)
        if path == "/api/status":
            return self._json(status)
        if path == "/":
            self.path = "/index.html"
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
    ap.add_argument("--no-collect", action="store_true", help="시작할 때 수집하지 않음")
    ap.add_argument("--open", action="store_true", help="브라우저 열기")
    a = ap.parse_args()
    if not a.no_collect:
        start_refresh()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print(f"배포 현황 보드: http://localhost:{a.port}  (Ctrl+C 로 종료)")
    if a.open:
        webbrowser.open(f"http://localhost:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
