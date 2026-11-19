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


def well_formed(d):
    """permissions 응답 모양 검사 → 문제 문구 또는 None. 모양이 틀린 응답을 허용으로 읽지 않게(빈 값은 「제외 없음」으로 읽히므로).
    실제 응답(11-19 7개 앱)은 항목마다 actions·notActions 가 문자열 목록이다."""
    if not isinstance(d, dict) or not isinstance(d.get("value"), list):
        return "value 목록 없음"
    for p in d["value"]:
        if not isinstance(p, dict):
            return "항목이 객체가 아님"
        for k in ("actions", "notActions"):
            v = p.get(k)
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return f"{k} 가 문자열 목록이 아님"
    return None


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
    if out["az"]["ok"]:
        # account show 는 이 PC 에 저장된 계정만 읽어 토큰이 만료돼도 성공한다 — 토큰을 실제로 받아 봐야 swap 권한 조회와 같은 답이 된다
        try:
            run(["az", "account", "get-access-token", "--query", "expiresOn", "-o", "tsv"], timeout=60)
        except Exception as e:
            out["az"] = {"ok": False, "user": out["az"]["user"], "sub": out["az"]["sub"],
                         "reason": "az 토큰을 못 받음(로그인 만료 또는 통신 오류) — az login 다시 — " + str(e)[:200]}
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
        self.perm_lock = threading.Lock()   # 권한 캐시 쓰기 — 주기 확인과 live_check 가 동시에 쓴다
        self.perm_seq, self.perm_at = 0, {}   # 조회 시작 번호, 앱별로 마지막에 적힌 결과의 시작 번호
        self.fake = os.environ.get("DEPLOY_BOARD_SWAP_CMD")
        self.status = {"enabled": True, "fake": bool(self.fake), "perms": {}, "perms_checked_at": None,
                       "perms_failed": 0, "perms_round": 0,
                       "job": None, "last": None, "undo": {}, "undo_minutes": UNDO_MINUTES,
                       "resource_group": cfg_loader().get("resource_group")}

    # ---------- 권한 ----------
    def site_id(self, cfg, sub, app):
        return f"/subscriptions/{sub}/resourceGroups/{cfg['resource_group']}/providers/Microsoft.Web/sites/{app}"

    def ticket(self):
        """조회를 시작할 때 번호를 받는다 — 늦게 시작한 조회의 결과가 이긴다."""
        with self.perm_lock:
            self.perm_seq += 1
            return self.perm_seq

    def put_perm(self, name, p, seq):
        """먼저 시작했지만 늦게 끝난 조회가 더 새 결과(예: 계정 전환 뒤 live_check 의 실패)를 덮지 않게 한다(11-19 Codex 재검토)."""
        with self.perm_lock:
            if self.perm_at.get(name, 0) > seq:
                return False
            self.perm_at[name] = seq
            self.status["perms"][name] = p
            self.status["perms_failed"] = sum(1 for v in self.status["perms"].values() if v.get("error"))
            return True

    def fail_all(self, why, names=(), seq=None):
        """확인 자체가 실패하면 예전 「허용」을 남기지 않는다 — 버튼이 지금 로그인과 다른 결과로 켜져 있지 않게.
        error=True 는 「권한 없음」이 아니라 「확인 못 함」(화면 문구가 다르다)."""
        seq = seq or self.ticket()
        for name in set(self.status["perms"]) | set(names):
            self.put_perm(name, {"ok": False, "error": True, "reason": why[:300]}, seq)

    def perm_of(self, cfg, sub, rc):
        """앱 하나의 swap 권한 → {"ok", "reason", "error"?}. az rest 는 토큰이 살아 있어야 되므로 로그인 확인도 겸한다.
        예외를 내지 않는다(응답 모양이 이상해도 「확인 실패」)."""
        try:
            url = f"https://management.azure.com{self.site_id(cfg, sub, rc['app'])}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"
            d = json.loads(collector.sh(["az", "rest", "--method", "get", "--url", url], timeout=60) or "{}")
            bad = well_formed(d)
            if bad:
                return {"ok": False, "error": True, "reason": f"권한 조회 응답이 예상과 다름 — {bad}"}
            ok, why = swap_allowed(d["value"])
            return {"ok": ok, "reason": why}
        except Exception as e:
            return {"ok": False, "error": True, "reason": f"권한 조회 실패 — {e}"[:300]}

    def check_perms(self, seq=None):
        seq = seq or self.ticket()
        try:
            cfg = self.cfg_loader()
            apps = [rc for rc in cfg["repos"] if rc.get("app")]
        except Exception as e:
            return self.fail_all(f"설정을 못 읽음 — {e}", seq=seq)
        try:
            sub = collector.Collector(cfg).subscription()
        except Exception as e:
            return self.fail_all(f"az 로그인 확인 실패 — {e}", [rc["name"] for rc in apps], seq)
        def one(rc):   # az 호출이 한 번에 10초 넘게 걸려 레포별로 동시에 묻는다
            self.put_perm(rc["name"], self.perm_of(cfg, sub, rc), seq)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, apps))
        self.status["perms_round"] += 1   # 화면은 시각(초 단위) 대신 이 번호로 「새로 확인함」을 안다
        self.status["perms_checked_at"] = now()

    def live_check(self, repo):
        """swap·되돌리기 직전(Slack 예고보다 먼저) 그 앱의 권한을 지금 로그인으로 다시 묻는다 → (ok, 이유).
        캐시는 1시간마다라 그 사이 로그아웃·토큰 만료·계정 전환·권한 회수가 있으면 버튼만 켜져 있다(11-19 Codex 검토).
        결과로 캐시도 고친다. 가짜 명령으로 시험할 때는 az 를 부르지 않는다."""
        if self.fake:
            return True, "가짜 명령"
        seq = self.ticket()
        try:
            cfg = self.cfg_loader()
            rc = next((r for r in cfg["repos"] if r["name"] == repo), None)
            if not rc or not rc.get("app"):
                return True, "swap 모드 레포가 아님"   # 거절은 can_start 가 한다
            sub = collector.Collector(cfg).subscription()
        except Exception as e:
            p = {"ok": False, "error": True, "reason": f"az 로그인 확인 실패 — {e}"[:300]}
        else:
            p = self.perm_of(cfg, sub, rc)
        self.put_perm(repo, p, seq)
        return p["ok"], p["reason"]

    def perm_loop(self, every_s=3600, before=None):
        """before: 같은 때에 로그인 표시도 다시 읽는다(헤더와 버튼이 다른 시점 결과를 보이지 않게)."""
        while True:
            self.recheck(before)
            time.sleep(every_s)

    def app_names(self):
        try:
            return [rc["name"] for rc in self.cfg_loader()["repos"] if rc.get("app")]
        except Exception:
            return []

    def begin_check(self):
        """다시 확인을 잡는다 — 이미 도는 중이면 False(겹쳐 누름 방지). 잡았으면 run_check 가 놓는다."""
        if not self.check_lock.acquire(blocking=False):
            return False
        self.status["checking"] = True
        return True

    def run_check(self, before=None):
        seq = self.ticket()   # 확인이 터져 전체 실패로 바꿀 때도 이 번호로 — 그 뒤에 시작한 live_check 결과를 덮지 않게
        try:
            acc = None
            if before:
                try:
                    acc = before()
                except Exception:
                    traceback.print_exc()
            az = (acc or {}).get("az") if isinstance(acc, dict) else None
            if isinstance(az, dict) and az.get("ok") is False:
                # 헤더가 로그인 실패를 보이는데 버튼이 앱별 조회(수십 초)가 끝날 때까지 예전 「허용」으로 남지 않게 바로 끈다
                return self.fail_all(f"az 로그인 확인 실패 — {az.get('reason') or '로그인 필요'}", self.app_names(), seq)
            self.check_perms(seq)
        except Exception as e:
            traceback.print_exc()
            self.fail_all(f"권한 확인 중 오류 — {e}", seq=seq)
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
        if not perm:
            return False, "swap 권한 조회 전", cfg, rc
        if perm.get("error"):
            return False, f"swap 권한을 확인하지 못함 — {perm['reason']}", cfg, rc
        if not perm.get("ok"):
            return False, f"swap 권한 없음 — {perm['reason']}", cfg, rc
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
