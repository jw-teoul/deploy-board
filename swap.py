"""내 PC 전용 swap 모드(`server.py --allow-swap`) — 권한 조회·swap 실행·되돌리기·기록.

기본 실행과 공유용(`--readonly`)에서는 이 모듈의 쓰기 기능을 쓰지 않는다. swap 은 실행한 사람의 az 로그인으로 나가므로
포털과 같은 권한·같은 Azure 활동 기록이 남는다.
시험할 때는 환경 변수 DEPLOY_BOARD_SWAP_CMD 에 가짜 명령(예: python -c "import time;time.sleep(3)")을 넣으면 az 대신 그것을 실행한다.
"""
import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

import collector

SWAP_ACTION = "Microsoft.Web/sites/slotsswap/Action"
UNDO_MINUTES = 30
TIMEOUT_S = 900


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def action_match(pattern, action):
    """RBAC 작업 패턴(* 와일드카드, 대소문자 무시)이 작업 이름에 맞는지."""
    rx = "^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$"
    return re.match(rx, action, re.IGNORECASE) is not None


def swap_allowed(perms, action=SWAP_ACTION):
    """permissions API 의 value 목록으로 작업 허용 여부를 판정한다 → (bool, 이유).
    역할(항목)마다 actions 중 하나에 맞고 그 항목의 notActions 에 안 걸리면 허용이고, 항목 중 하나라도 허용하면 허용이다."""
    blocked_by = None
    for p in perms or []:
        allow = next((a for a in p.get("actions") or [] if action_match(a, action)), None)
        if not allow:
            continue
        deny = next((n for n in p.get("notActions") or [] if action_match(n, action)), None)
        if deny:
            blocked_by = blocked_by or f"'{allow}' 역할이 '{deny}' 를 제외함"
            continue
        return True, f"'{allow}' 로 허용"
    return False, blocked_by or "swap 을 허용하는 역할 없음"


def whoami(run=None):
    """이 PC 의 az·gh 로그인 상태 → {"az": {...}, "gh": {...}, "checked_at"}. 로그인은 하지 않고 확인만 한다.
    run 은 시험용(collector.sh 대체)."""
    run = run or collector.sh
    out = {"checked_at": now()}
    try:
        d = json.loads(run(["az", "account", "show", "--query", "{user:user.name, sub:name}", "-o", "json"], timeout=30) or "{}")
        out["az"] = {"ok": True, "user": d.get("user"), "sub": d.get("sub")}
    except Exception as e:
        out["az"] = {"ok": False, "reason": "az login 필요 또는 확인 실패 — " + str(e)[:200]}
    try:
        out["gh"] = {"ok": True, "user": run(["gh", "api", "user", "--jq", ".login"], timeout=30).strip()}
    except Exception as e:
        out["gh"] = {"ok": False, "reason": "gh auth login 필요 또는 확인 실패 — " + str(e)[:200]}
    return out


class SwapRunner:
    """swap·되돌리기를 한 번에 한 건만 백그라운드로 실행하고 상태를 status dict 에 비춘다."""

    def __init__(self, cfg_loader, log_path, on_done, notify=None):
        """notify(done) → dict|None: 끝난 기록을 남기기 전에 부른다(Slack 결과 전송). 돌려준 dict 는 기록의 slack 칸에 합친다."""
        self.cfg_loader, self.log_path, self.on_done, self.notify = cfg_loader, log_path, on_done, notify
        self.lock = threading.Lock()
        self.check_lock = threading.Lock()
        self.fake = os.environ.get("DEPLOY_BOARD_SWAP_CMD")
        self.status = {"enabled": True, "fake": bool(self.fake), "perms": {}, "perms_checked_at": None,
                       "job": None, "last": None, "undo": {}, "undo_minutes": UNDO_MINUTES,
                       "resource_group": cfg_loader().get("resource_group")}

    # ---------- 권한 ----------
    def site_id(self, cfg, sub, app):
        return f"/subscriptions/{sub}/resourceGroups/{cfg['resource_group']}/providers/Microsoft.Web/sites/{app}"

    def check_perms(self):
        cfg = self.cfg_loader()
        try:
            sub = collector.Collector(cfg).subscription()
        except Exception as e:
            for rc in cfg["repos"]:
                if rc.get("app"):
                    self.status["perms"][rc["name"]] = {"ok": False, "reason": f"az 로그인 확인 실패 — {e}"[:300]}
            return
        def one(rc):   # az 호출이 한 번에 10초 넘게 걸려 레포별로 동시에 묻는다
            url = f"https://management.azure.com{self.site_id(cfg, sub, rc['app'])}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"
            try:
                d = json.loads(collector.sh(["az", "rest", "--method", "get", "--url", url], timeout=60) or "{}")
                ok, why = swap_allowed(d.get("value"))
                self.status["perms"][rc["name"]] = {"ok": ok, "reason": why}
            except Exception as e:
                self.status["perms"][rc["name"]] = {"ok": False, "reason": f"권한 조회 실패 — {e}"[:300]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, [rc for rc in cfg["repos"] if rc.get("app")]))
        self.status["perms_checked_at"] = now()

    def perm_loop(self, every_s=3600):
        while True:
            self.recheck()
            time.sleep(every_s)

    def begin_check(self):
        """다시 확인을 잡는다 — 이미 도는 중이면 False(겹쳐 누름 방지). 잡았으면 run_check 가 놓는다."""
        if not self.check_lock.acquire(blocking=False):
            return False
        self.status["checking"] = True
        return True

    def run_check(self, before=None):
        try:
            if before:
                before()
            self.check_perms()
        except Exception:
            traceback.print_exc()
        finally:
            self.status["checking"] = False
            self.check_lock.release()

    def recheck(self, before=None):
        if not self.begin_check():
            return False
        self.run_check(before)
        return True

    # ---------- 실행 ----------
    def command(self, cfg, app):
        return ["az", "webapp", "deployment", "slot", "swap", "-g", cfg["resource_group"], "-n", app,
                "--slot", "staging", "--target-slot", "production"]

    def undo_open(self, repo):
        u = self.status["undo"].get(repo)
        if u and u["until"] > now():
            return True
        self.status["undo"].pop(repo, None)
        return False

    def can_start(self, repo, action):
        """→ (시작할 수 있나, 이유, 설정, 레포 설정). Slack 예고를 보내기 전에 미리 확인하는 데도 쓴다."""
        cfg = self.cfg_loader()
        rc = next((r for r in cfg["repos"] if r["name"] == repo), None)
        if not rc or not rc.get("app"):
            return False, "swap 모드 레포가 아님", cfg, rc
        if action not in ("swap", "undo"):
            return False, "알 수 없는 동작", cfg, rc
        perm = self.status["perms"].get(repo)
        if not perm or not perm.get("ok"):
            return False, "swap 권한 없음 또는 조회 전" + (f" — {perm['reason']}" if perm else ""), cfg, rc
        if action == "undo" and not self.undo_open(repo):
            return False, f"되돌릴 swap 이 없음(이 보드에서 {UNDO_MINUTES}분 안에 한 swap 만)", cfg, rc
        if self.status["job"]:
            return False, f"다른 swap 진행 중({self.status['job']['repo']})", cfg, rc
        return True, "시작 가능", cfg, rc

    def start(self, repo, action, info=None):
        """→ (시작했나, 이유). action 은 swap | undo."""
        ok, why, cfg, rc = self.can_start(repo, action)
        if not ok:
            return False, why
        with self.lock:
            if self.status["job"]:
                return False, f"다른 swap 진행 중({self.status['job']['repo']})"
            cmd = self.command(cfg, rc["app"])
            job = {"id": uuid.uuid4().hex[:8], "repo": repo, "app": rc["app"], "action": action, "started_at": now(),
                   "cmd": " ".join(cmd), "fake": bool(self.fake), "info": info or {}}
            self.status["job"] = job
        threading.Thread(target=self._run, args=(job, cmd), daemon=True).start()
        return True, "시작"

    def _run(self, job, cmd):
        res = {"ok": False, "rc": None, "error": None}
        try:
            if self.fake:
                p = subprocess.run(self.fake, shell=True, capture_output=True, timeout=TIMEOUT_S)
            else:
                env = dict(os.environ, PYTHONIOENCODING="utf-8")
                p = subprocess.run([collector.exe("az")] + cmd[1:], capture_output=True, timeout=TIMEOUT_S, env=env)
            res["rc"] = p.returncode
            res["ok"] = p.returncode == 0
            if not res["ok"]:
                res["error"] = p.stderr.decode("utf-8", "replace")[-600:] or f"rc={p.returncode}"
        except subprocess.TimeoutExpired:
            res["error"] = f"{TIMEOUT_S}초 안에 끝나지 않음 — 포털·활동 기록에서 상태 확인"
        except Exception as e:
            res["error"] = str(e)[-600:]
        done = dict(job, **res, finished_at=now())
        if res["ok"] and job["action"] == "swap":
            until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=UNDO_MINUTES)).isoformat(timespec="seconds")
            self.status["undo"][job["repo"]] = {"at": done["finished_at"], "until": until, "id": job["id"]}
            if (job["info"] or {}).get("slack"):   # 되돌리기 결과도 같은 Slack 스레드에 단다
                self.status["undo"][job["repo"]]["slack"] = dict(job["info"]["slack"])
        elif res["ok"] and job["action"] == "undo":
            self.status["undo"].pop(job["repo"], None)
        if self.notify:
            try:
                extra = self.notify(done)
                if extra:
                    done["slack"] = extra
            except Exception:
                traceback.print_exc()
        self._log(done)
        with self.lock:
            self.status["last"] = done
            self.status["job"] = None
        try:
            self.on_done(job["repo"])
        except Exception:
            traceback.print_exc()

    def _log(self, rec):
        rec = dict(rec)
        try:
            rec["by"] = "가짜 명령" if self.fake else collector.sh(["az", "account", "show", "--query", "user.name", "-o", "tsv"], timeout=30).strip()
        except Exception:
            rec["by"] = None
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
