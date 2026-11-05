"""배포 현황 수집기 — 레포별 develop/스테이징/운영 상태와 Jira 작업 브랜치 단계를 모아 data/state.json 에 쓴다.

실행: python collector.py [--repo adminsite] [--out data/state.json]
원천: 로컬 클론(git fetch → log --cherry-pick·for-each-ref·merge-base), gh pr list·gh run list, az monitor activity-log(slotsSwap).
레포 모드: `app` 이 있으면 swap 모드(deploy/prod = staging 슬롯, 운영 = swap), `prod_branch` 가 있으면 브랜치 모드
(스테이징 브랜치·운영 브랜치 머지가 곧 배포, 운영 = 운영 브랜치 마지막 성공 run). 클론이 없으면 `../projects/<repo>` 에 받는다.
"""
import argparse
import datetime as dt
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = ["branch", "pr", "develop", "staging", "prod"]  # 낮은 → 높은 단계
STAGE_LABEL = {"branch": "브랜치", "pr": "PR", "develop": "develop", "staging": "스테이징", "prod": "운영"}
SEP = "\x1f"
_EXE = {}


def exe(name):
    """gh.exe / az.cmd / git.exe 의 실제 경로(Windows 에서 shell 없이 실행하기 위해)."""
    if name not in _EXE:
        _EXE[name] = shutil.which(name) or name
    return _EXE[name]


def sh(args, timeout=120, cwd=None):
    """CLI 실행(shell 없이 — 인자 안의 ?·따옴표가 깨지지 않게). 실패하면 RuntimeError(표준오류 포함)."""
    env = dict(os.environ, MSYS_NO_PATHCONV="1", PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0")
    args = [exe(args[0])] + list(args[1:])
    p = subprocess.run(args, capture_output=True, timeout=timeout, env=env, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}… rc={p.returncode}: {p.stderr.decode('utf-8', 'replace')[:400]}")
    try:
        return p.stdout.decode("utf-8")
    except UnicodeDecodeError:   # Windows 의 az.cmd 는 PYTHONIOENCODING 을 무시하고 cp949 로 내보낼 때가 있다(구독 이름 등 한글)
        return p.stdout.decode(locale.getpreferredencoding(False) or "utf-8", "replace")


def gh_json(args, timeout=120):
    return json.loads(sh(["gh"] + args, timeout) or "null")


def az_json(args, timeout=180):
    return json.loads(sh(["az"] + args + ["-o", "json"], timeout) or "null")


SAME_SWAP_S = 30   # 사이트 slotSwapStatus 와 활동 기록을 같은 swap 으로 보는 허용 차이(초)
INCOMPLETE_SWAP_S = 10 * 60   # 활동 기록에 시작 이벤트만 있는 swap 은 그 뒤 이만큼 안의 사이트 값을 같은 swap 으로 본다


def iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class Repo:
    """로컬 클론 하나에 대한 git 조회. checkout·커밋은 하지 않는다(fetch 만)."""

    def __init__(self, path, org, name):
        self.path = path
        self.org, self.name = org, name
        self.web = f"https://github.com/{org}/{name}"

    def git(self, *args, timeout=120):
        return sh(["git", "-C", self.path] + list(args), timeout=timeout)

    def ensure(self):
        """클론이 없으면 받고(gh repo clone — SSH/HTTPS 는 gh 설정을 따른다), 단일 브랜치 클론이면 모든 브랜치를 받도록 refspec 을 넓힌다.
        돌려주는 값은 사람에게 보일 메모(없으면 None)."""
        note = None
        if not os.path.isdir(os.path.join(self.path, ".git")):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            sh(["gh", "repo", "clone", f"{self.org}/{self.name}", self.path, "--", "--quiet"], timeout=900)
            note = f"클론 새로 받음: {self.path}"
        spec = "+refs/heads/*:refs/remotes/origin/*"
        try:
            cur = self.git("config", "--get-all", "remote.origin.fetch").split()
        except RuntimeError:
            cur = []
        if spec not in cur:
            self.git("config", "remote.origin.fetch", spec)
            note = (note + " · " if note else "") + "단일 브랜치 클론이라 fetch refspec 을 전체 브랜치로 넓힘"
        return note

    def fetch(self):
        self.git("fetch", "origin", "--prune", "--quiet", timeout=300)

    def head(self, ref):
        out = self.git("log", "-1", f"--format=%H{SEP}%cI{SEP}%s", ref).strip()
        sha, date, msg = out.split(SEP, 2)
        return {"sha": sha, "date": date, "message": msg}

    def only_in(self, base, head, keys_of):
        """base 에 없고 head 에만 있는 커밋 — 패치 내용이 같은 cherry-pick 은 제외(--cherry-pick), 머지 커밋 제외."""
        out = self.git("log", "--no-merges", "--cherry-pick", "--right-only",
                       f"--format=%H{SEP}%an{SEP}%cI{SEP}%s", f"{base}...{head}")
        commits = []
        for line in out.splitlines():
            if not line.strip():
                continue
            sha, author, date, msg = line.split(SEP, 3)
            commits.append({"sha": sha, "author": author, "date": date, "message": msg,
                            "url": f"{self.web}/commit/{sha}", "keys": keys_of(msg)})
        return {"commits": commits}

    def branches(self, keys_of):
        out = self.git("for-each-ref", f"--format=%(refname:short){SEP}%(objectname){SEP}%(committerdate:iso-strict){SEP}%(authorname){SEP}%(subject)",
                       "refs/remotes/origin/")
        res = []
        for line in out.splitlines():
            if not line.strip():
                continue
            ref, sha, date, author, msg = line.split(SEP, 4)
            name = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
            if name == "HEAD":
                continue
            res.append({"name": name, "sha": sha, "date": date, "author": author, "message": msg, "keys": keys_of(name)})
        return res

    def is_ancestor(self, a, b):
        """a 가 b 의 조상(= b 에 포함)인가."""
        try:
            self.git("merge-base", "--is-ancestor", a, b)
            return True
        except RuntimeError:
            return False


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.org = cfg["org"]
        self.key_re = re.compile(cfg.get("issue_key_pattern", r"[A-Z][A-Z0-9]+-\d+"))
        self.errors = []
        self._sub = None
        self._site_swaps = None

    def keys_of(self, text):
        return sorted(set(self.key_re.findall(text or "")))

    # ---------- GitHub (gh) ----------
    def prs(self, repo):
        fields = "number,title,headRefName,baseRefName,state,isDraft,mergedAt,mergeCommit,url,updatedAt,author"
        d = gh_json(["pr", "list", "-R", f"{self.org}/{repo}", "--state", "all", "-L", "300", "--json", fields])
        for p in d:
            p["mergeCommit"] = (p.get("mergeCommit") or {}).get("oid")
            p["author"] = (p.get("author") or {}).get("login")
            p["keys"] = sorted(set(self.keys_of(p["headRefName"]) + self.keys_of(p["title"])))
        return d

    def deploy_runs(self, repo, workflow, branch="deploy/prod"):
        fields = "headSha,conclusion,status,createdAt,updatedAt,url,displayTitle,headBranch"
        return gh_json(["run", "list", "-R", f"{self.org}/{repo}", "--workflow", workflow, "-b", branch,
                        "-L", "30", "--json", fields])

    # ---------- Azure (az) ----------
    def subscription(self):
        if self._sub is None:
            self._sub = sh(["az", "account", "show", "--query", "id", "-o", "tsv"]).strip()
        return self._sub

    def swaps(self, app, days=None):
        """성공한 slot swap 목록(최신순). 작업 이름은 slotsswap/SlotsSwap 으로 대소문자가 섞여 있고,
        사람 호출과 SlotSwapJobProcessor 호출이 같은 swap 에 둘 다 남는다 → merge_swap_events 로 합친다."""
        rid = (f"/subscriptions/{self.subscription()}/resourceGroups/{self.cfg['resource_group']}"
               f"/providers/Microsoft.Web/sites/{app}")
        days = int(days or self.cfg.get("swap_lookback_days", 90))
        # 기본 --max-events 50 이면 배포가 잦은 날 swap 이 최근 50건 밖으로 밀려 안 보인다(10-24 사고) → 넉넉히 올리고 필드만 줄인다
        d = az_json(["monitor", "activity-log", "list", "--resource-id", rid, "--offset", f"{days}d", "--max-events", "5000",
                     "--select", "eventTimestamp", "operationName", "status", "caller", "correlationId"], timeout=300)
        events = []
        for e in d or []:
            op = ((e.get("operationName") or {}).get("value") or "").lower()
            if "slotsswap" in op and (e.get("status") or {}).get("value") == "Succeeded":
                events.append({"time": e["eventTimestamp"], "who": e.get("caller") or "", "cid": e.get("correlationId") or ""})
        return self.merge_swap_events(events)

    @staticmethod
    def merge_swap_events(events, gap_s=180):
        """같은 swap 의 이벤트를 하나로: correlationId 가 같으면 같은 swap(11-05 실측 35건 모두 사람·처리 이벤트가 같은 값,
        두 이벤트가 2분 벌어진 경우도 있음). correlationId 가 없는 이벤트만 묶음 첫 시각에서 gap_s 안이면 합친다
        (마지막 시각 기준이면 줄줄이 이어 붙어 서로 다른 swap 이 합쳐진다). 사람 caller 를 남기고 시각은 늦은 쪽(최신순).
        start(첫 이벤트 시각)·n(이벤트 수)도 남긴다 — n=1 이면 처리 이벤트가 아직 안 올라온 swap 일 수 있다(pending_swap)."""
        def add(g, ev):
            if "@" in ev["who"] and "@" not in g["who"]:
                g["who"] = ev["who"]
            g["time"] = max(g["time"], ev["time"])
        by_cid, loose = {}, []
        for ev in sorted(events, key=lambda x: x["time"]):
            cid = ev.get("cid")
            if cid:
                if cid in by_cid:
                    add(by_cid[cid], ev)
                else:
                    by_cid[cid] = {"time": ev["time"], "who": ev["who"], "start": ev["time"], "n": 0}
                by_cid[cid]["n"] += 1
            elif loose and (iso(ev["time"]) - iso(loose[-1]["start"])).total_seconds() < gap_s:
                add(loose[-1], ev)
                loose[-1]["n"] += 1
            else:
                loose.append({"time": ev["time"], "who": ev["who"], "start": ev["time"], "n": 1})
        out = list(by_cid.values()) + loose
        return sorted(out, key=lambda x: x["time"], reverse=True)

    def site_swaps(self):
        """리소스 그룹 사이트 전체의 마지막 성공 swap(slotSwapStatus) — az 1회, 수집 한 번 안에서는 캐시.
        활동 기록보다 3~5분 빨리 바뀐다(11-05 실측: 활동 기록 성공 시각보다 6~9초 앞선 값). {앱: {time, from, to}}"""
        if isinstance(self._site_swaps, Exception):   # 실패도 캐시 — 한 수집에서 앱마다 다시 부르지 않는다
            raise self._site_swaps
        if self._site_swaps is None:
            try:
                d = az_json(["webapp", "list", "-g", self.cfg["resource_group"],
                             "--query", "[].{name:name, s:slotSwapStatus}"], timeout=120)
            except Exception as e:
                self._site_swaps = e
                raise
            out = {}
            for x in d or []:
                s = x.get("s") or {}
                if not (x.get("name") and s.get("timestampUtc")):
                    continue
                try:   # 한 항목이 이상해도 다른 앱은 본다. 시각은 UTC 표기가 있는 값만 쓴다
                    t = iso(s["timestampUtc"])
                    if t.tzinfo is None:
                        continue
                except ValueError:
                    continue
                out[x["name"]] = {"time": t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                                  "from": s.get("sourceSlotName") or "", "to": s.get("destinationSlotName") or ""}
            self._site_swaps = out
        return self._site_swaps

    @staticmethod
    def pending_swap(site, swaps, gap_s=SAME_SWAP_S, open_s=INCOMPLETE_SWAP_S):
        """사이트의 마지막 swap 이 운영 대상이고 활동 기록 최신 swap 보다 gap_s 넘게 새로우면 잠정 swap 으로 본다.
        같은 swap 은 활동 기록이 사이트 값보다 몇 초 뒤라(11-05 실측 6~9초) 활동 기록이 오면 저절로 풀린다.
        gap_s 를 넓게 잡으면 짧은 간격의 다음 swap(되돌리기)을 앞 swap 기록으로 오인한다.
        단 활동 기록 최신 swap 이 이벤트 1건뿐이면(시작 쪽만 올라오고 처리 이벤트가 아직 — 11-04 처럼 2분 벌어짐)
        그 시작에서 open_s 안의 사이트 값은 같은 swap 으로 본다. 겹쳐 세면 운영·스테이징 추적이 한 번 더 뒤집힌다."""
        if not site or (site.get("to") or "").lower() != "production":
            return None
        t = iso(site.get("time"))
        if not t:
            return None
        if swaps:
            if (t - iso(swaps[0]["time"])).total_seconds() <= gap_s:
                return None
            if swaps[0].get("n", 2) < 2 and (t - iso(swaps[0].get("start") or swaps[0]["time"])).total_seconds() <= open_s:
                return None
        return {"time": site["time"], "from": site.get("from", ""), "to": site["to"]}

    def slot_hosts(self, app):
        try:
            prod = az_json(["webapp", "show", "-g", self.cfg["resource_group"], "-n", app, "--query", "defaultHostName"])
            slots = az_json(["webapp", "deployment", "slot", "list", "-g", self.cfg["resource_group"], "-n", app])
        except Exception as e:  # 호스트는 부가 정보 — 실패해도 진행
            self.errors.append(f"{app}: slot host 조회 실패 — {e}")
            return {"prod": None, "staging": None}
        st = next((s["defaultHostName"] for s in slots or [] if s["name"] == "staging"), None)
        return {"prod": prod, "staging": st}

    # ---------- 판정 ----------
    @staticmethod
    def estimate_prod(runs, swaps, since=None):
        """운영 SHA: 성공 배포 run(스테이징 슬롯에 들어감)과 swap(운영·스테이징 맞바꿈)을 시각순으로 따라간다.
        배포 없이 swap 을 다시 하면(되돌리기) 이전 운영으로 돌아간 것까지 맞는다. 추적이 조회 범위(run 30건) 밖을
        가리키면 「마지막 swap 이전 마지막 성공 run」 으로 단순 추정한다.
        since(swap 조회 시작 시각)보다 오래된 run 은 추적에 넣지 않는다 — 그 사이 swap 을 몰라 순서가 어긋날 수 있다."""
        if not swaps:
            return None, "swap 기록 없음(조회 기간 안)"
        ok = [r for r in runs if r.get("conclusion") == "success" and iso(r.get("updatedAt"))]
        tracked = [r for r in ok if not since or iso(r["updatedAt"]) >= since]
        ev = [(iso(r["updatedAt"]), 0, r) for r in tracked] + [(iso(s["time"]), 1, s) for s in swaps if iso(s.get("time"))]
        prod = stg = None   # (sha, run updatedAt) — None 은 조회 범위 밖이라 모름
        for _, kind, x in sorted(ev, key=lambda e: (e[0], e[1])):
            if kind == 0:
                stg = (x["headSha"], x["updatedAt"])
            else:
                prod, stg = stg, prod
        last = swaps[0]["time"][:19]
        if prod:
            return prod[0], f"배포·swap 순서 추적(swap {len(swaps)}회) — 마지막 swap {last}Z 뒤 운영 = 성공 배포 run({prod[1]})"
        last_t = iso(swaps[0]["time"])
        for r in ok:  # gh run list 는 최신순
            if iso(r["updatedAt"]) <= last_t:
                return r["headSha"], f"추적 범위 밖 — 단순 추정: 마지막 swap {last}Z 이전 마지막 성공 배포 run({r['updatedAt']})"
        return None, "마지막 swap 이전 성공 배포 run 을 조회 범위(30건)에서 못 찾음"

    @staticmethod
    def prod_from_runs(runs):
        """브랜치 모드 운영 SHA: 운영 브랜치의 마지막 성공 배포 run 의 headSha(머지가 곧 배포인 레포 — Vercel 등)."""
        for r in runs:
            if r.get("conclusion") == "success":
                return r["headSha"], f"운영 브랜치 마지막 성공 배포 run({r['updatedAt']})"
        return None, "운영 브랜치 성공 배포 run 을 조회 범위(30건)에서 못 찾음"

    # ---------- 변경 감지(가벼운 조회) ----------
    def probe_repo(self, rc):
        """레포가 바뀌었는지 싸게 본다: 원격 브랜치 끝 커밋 전체(ls-remote 1회) + 가장 최근 갱신된 PR 시각(API 1회)."""
        g = Repo(self.local_path(rc), self.org, rc["name"])
        refs = g.git("ls-remote", "--heads", "origin", timeout=60)
        import hashlib
        h = hashlib.sha1(refs.encode("utf-8")).hexdigest()[:12]
        pr = gh_json(["api", f"repos/{self.org}/{rc['name']}/pulls?state=all&sort=updated&direction=desc&per_page=1"], timeout=60)
        pr_at = pr[0]["updated_at"] if pr else None
        fp = {"refs": h, "pr": pr_at}
        if rc.get("prod_branch"):  # 브랜치 모드는 배포 run 이 끝나야 운영이 바뀌므로 마지막 run 상태도 본다
            run = gh_json(["run", "list", "-R", f"{self.org}/{rc['name']}", "--workflow", rc.get("workflow", "deploy.yml"),
                           "-b", rc["prod_branch"], "-L", "1", "--json", "updatedAt,status,conclusion"], timeout=60)
            fp["run"] = f"{run[0]['updatedAt']} {run[0]['status']} {run[0]['conclusion']}" if run else None
        return fp

    def probe_swap(self, app):
        """마지막 성공 swap 시각(활동 기록 1회, 최근 7일만 — 전체 90일은 17초라 감시엔 무겁다)."""
        sw = self.swaps(app, days=7)
        return sw[0]["time"] if sw else None

    def local_path(self, rc):
        p = rc.get("local") or os.path.join(HERE, "..", "projects", rc["name"])
        return os.path.abspath(p)

    def collect_repo(self, rc):
        name, app = rc["name"], rc.get("app")
        stg, prd = rc.get("staging_branch", "deploy/prod"), rc.get("prod_branch")
        t0 = time.time()
        r = {"name": name, "label": rc.get("label", name), "app": app, "errors": [], "notes": [],
             "staging_branch": stg, "prod_branch": prd, "mode": "swap" if app else ("branch" if prd else "none"),
             "web": f"https://github.com/{self.org}/{name}"}
        g = Repo(self.local_path(rc), self.org, name)
        try:
            note = g.ensure()
            if note:
                r["notes"].append(note)
                print(f"[collect] {name}: {note}", file=sys.stderr, flush=True)
        except Exception as e:
            r["errors"].append(f"로컬 클론 준비 실패({g.path}) — {e}")
            self.errors.append(f"{name}: 로컬 클론 준비 실패 {e}")
            return r
        try:
            g.fetch()
            r["develop"] = g.head("origin/develop")
            r["staging"] = g.head(f"origin/{stg}")
            r["develop_only"] = g.only_in(f"origin/{stg}", "origin/develop", self.keys_of)
            r["branches"] = [b for b in g.branches(self.keys_of) if b["keys"]]
        except Exception as e:
            r["errors"].append(f"git 수집 실패 — {e}")
            self.errors.append(f"{name}: {e}")
            return r
        try:
            r["runs"] = self.deploy_runs(name, rc.get("workflow", "deploy.yml"), prd or stg)
            r["prs"] = self.prs(name)
        except Exception as e:
            r["errors"].append(f"GitHub 수집 실패 — {e}")
            self.errors.append(f"{name}: {e}")
            return r
        r["swaps"] = []
        r["prod"] = {"sha": None, "basis": "Azure 앱도 운영 브랜치도 미설정"}
        if app:  # swap 모드: 스테이징 브랜치가 staging 슬롯, 운영 = 마지막 swap 이전 성공 run
            try:
                since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(self.cfg.get("swap_lookback_days", 90)))
                r["swaps"] = self.swaps(app)
                try:   # 잠정 swap 은 부가 정보 — 조회 실패면 활동 기록만으로 판정(기존 동작)
                    pend = self.pending_swap(self.site_swaps().get(app), r["swaps"])
                except Exception as e:
                    pend = None
                    r["notes"].append(f"사이트 swap 상태 조회 실패 — {e}")
                if pend:
                    r["pending_swap"] = pend
                    sha, basis = self.estimate_prod(r["runs"], [pend] + r["swaps"], since)
                    basis = "잠정(Azure 활동 기록 확인 전) — " + basis
                else:
                    sha, basis = self.estimate_prod(r["runs"], r["swaps"], since)
                r["prod"] = {"sha": sha, "basis": basis}
                r["hosts"] = self.slot_hosts(app)
            except Exception as e:
                r["errors"].append(f"Azure 수집 실패 — {e}")
                self.errors.append(f"{name}: {e}")
        elif prd:  # 브랜치 모드: 운영 브랜치 머지가 곧 배포, 운영 = 그 브랜치 마지막 성공 run
            sha, basis = self.prod_from_runs(r["runs"])
            r["prod"] = {"sha": sha, "basis": basis}
            r["hosts"] = rc.get("hosts") or {}
        r["staging_only"] = None
        if r["prod"]["sha"]:
            try:
                r["staging_only"] = g.only_in(r["prod"]["sha"], f"origin/{stg}", self.keys_of)
                r["prod"]["date"] = g.head(r["prod"]["sha"])["date"]
            except Exception as e:
                r["errors"].append(f"스테이징·운영 비교 실패 — {e}")
        r["issues"] = self.judge_issues(r, g)
        r["duration_s"] = round(time.time() - t0, 1)
        r["collected_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        return r

    def judge_issues(self, r, g):
        """이슈 키별로 브랜치·PR·커밋을 모아 최고 단계와 꼬리표를 정한다."""
        issues = {}

        def get(k):
            return issues.setdefault(k, {"key": k, "branches": [], "prs": [], "develop_only": [], "staging_only": [],
                                         "stages": set(), "flags": []})

        prod_sha = (r.get("prod") or {}).get("sha")
        so = r.get("staging_only")
        stg, prd = r.get("staging_branch", "deploy/prod"), r.get("prod_branch")
        for b in r["branches"]:
            for k in b["keys"]:
                i = get(k)
                i["branches"].append({"name": b["name"], "date": b["date"], "author": b["author"], "message": b["message"],
                                      "url": f"{r['web']}/tree/{b['name']}"})
                i["stages"].add("branch")
        for c in r["develop_only"]["commits"]:
            for k in c["keys"]:
                i = get(k)
                i["develop_only"].append(c)
                i["stages"].add("develop")
        if so:
            for c in so["commits"]:
                for k in c["keys"]:
                    i = get(k)
                    i["staging_only"].append(c)
                    i["stages"].add("staging")
        for p in r["prs"]:
            for k in p["keys"]:
                i = get(k)
                i["prs"].append(p)
                if p["state"] == "OPEN":
                    i["stages"].add("pr")
                elif p["state"] == "MERGED":
                    if p["baseRefName"] == "develop":
                        i["stages"].add("develop")
                    elif p["baseRefName"] in (stg, prd):
                        i["stages"].add("staging")
                        # 머지 커밋이 운영 SHA 에 포함되면 운영 반영(swap 모드는 스테이징 브랜치 = 배포 브랜치)
                        if prod_sha and p["mergeCommit"] and g.is_ancestor(p["mergeCommit"], prod_sha):
                            i["stages"].add("prod")
        out = []
        for k, i in issues.items():
            st = i["stages"]
            stage = max(st, key=STAGES.index) if st else "branch"
            if stage == "prod" and i["staging_only"]:
                stage = "staging"
                i["flags"].append("운영 반영 뒤 추가 커밋이 스테이징에 있음")
            if stage in ("staging", "prod") and i["develop_only"]:
                i["flags"].append("develop 에만 있는 커밋 있음(운영 미반영)")
            if any(p["state"] == "OPEN" and p["isDraft"] for p in i["prs"]):
                i["flags"].append("Draft PR")
            if any(b["name"].startswith("stg/") for b in i["branches"]):
                i["flags"].append("stg/ 브랜치")
            dates = ([b["date"] for b in i["branches"] if b["date"]] + [p["updatedAt"] for p in i["prs"]]
                     + [c["date"] for c in i["develop_only"] + i["staging_only"]])
            i["last_activity"] = max(dates) if dates else None
            i["stage"] = stage
            i["stage_label"] = STAGE_LABEL[stage]
            i["stages"] = sorted(st, key=STAGES.index)
            i["title"] = next((p["title"] for p in sorted(i["prs"], key=lambda p: p["updatedAt"], reverse=True)), None) \
                or next((c["message"] for c in i["staging_only"] + i["develop_only"]), None) \
                or next((b["message"] for b in i["branches"]), None)
            out.append(i)
        out.sort(key=lambda x: (x["last_activity"] or ""), reverse=True)
        return out

    def collect(self, only=None):
        t0 = time.time()
        repos = [rc for rc in self.cfg["repos"] if not only or rc["name"] in only]
        state = {"collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "jira_base": self.cfg.get("jira_base"), "org": self.org, "repos": []}
        for rc in repos:
            print(f"[collect] {rc['name']} …", file=sys.stderr, flush=True)
            state["repos"].append(self.collect_repo(rc))
        state["errors"] = self.errors
        state["duration_s"] = round(time.time() - t0, 1)
        if self._sub:   # 웹 로그인 swap 화면이 ARM 주소를 만들 때 쓴다(구독 ID·리소스 그룹은 비밀이 아님)
            state["azure"] = {"subscription_id": self._sub, "resource_group": self.cfg.get("resource_group")}
        return state


def load_cfg(path=None):
    with open(path or os.path.join(HERE, "repos.json"), encoding="utf-8") as f:
        return json.load(f)


def merge_state(new, out):
    """일부 레포만 수집한 결과를 기존 state.json 에 합친다(같은 이름은 교체, 나머지는 유지)."""
    try:
        with open(out, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return new
    names = {r["name"] for r in new["repos"]}
    merged = dict(old)
    merged["repos"] = [r for r in old.get("repos", []) if r["name"] not in names] + new["repos"]
    order = {rc["name"]: i for i, rc in enumerate(load_cfg()["repos"])}
    merged["repos"].sort(key=lambda r: order.get(r["name"], 99))
    merged["collected_at"] = new["collected_at"]
    merged["duration_s"] = new["duration_s"]
    if new.get("azure"):
        merged["azure"] = new["azure"]
    merged["errors"] = [e for e in old.get("errors", []) if not any(e.startswith(n + ":") for n in names)] + new.get("errors", [])
    return merged


def write_state(state, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    for i in range(5):   # Windows: 다른 쪽(올리기·화면 읽기)이 잠깐 열고 있으면 교체가 PermissionError — 짧게 다시
        try:
            os.replace(tmp, out)
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.2 * (i + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", action="append", help="이 레포만(반복 가능)")
    ap.add_argument("--out", default=os.path.join(HERE, "data", "state.json"))
    ap.add_argument("--config")
    a = ap.parse_args()
    state = Collector(load_cfg(a.config)).collect(a.repo)
    write_state(state, a.out)
    for r in state["repos"]:
        print(f"{r['name']}: issues={len(r.get('issues', []))} develop_only={len(r.get('develop_only', {}).get('commits', []))} "
              f"staging_only={len((r.get('staging_only') or {}).get('commits', []))} prod={str(r.get('prod', {}).get('sha'))[:8]} "
              f"errors={r['errors']} {r.get('duration_s')}s", file=sys.stderr)
    print(f"errors={state['errors']} total={state['duration_s']}s -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
