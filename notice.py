"""배포 알림 문구(Slack mrkdwn) — 문구만 만든다. 보내기는 server(swap 예고·결과, 「알림 문구」 창의 보내기 버튼)가 한다.

- 예고: 지금 스테이징에만 있는 커밋(= swap 하면 운영에 나갈 것)을 이슈 키별로 묶는다.
- 결과: swap 할 때 swap-log 에 남긴 스냅숏(그 순간 나간 것)으로 「반영됨」 문구를 만든다.
  보드 밖(Portal) swap 은 수집한 swap 기록과 운영 배포 run 제목으로 초안을 만든다(outside_swap).
제목은 Jira 를 읽지 않으므로 PR 제목 → 커밋 첫 줄 순서로 쓴다(collector.judge_issues 의 title).
사내 글 형식: 첫 줄 결론, Slack 300자·8줄 안, `*굵게*`·`•` 한 줄 불릿, 표·`**` 없음.
"""
import datetime as dt
import json
import os
import re

MAX_ITEMS = 5      # 불릿 수(넘으면 「외 N건」)
MAX_CHARS = 300
MAX_LINES = 8
KST = dt.timezone(dt.timedelta(hours=9))

ADD_RX = re.compile(r"^(feat|feature|add)\b|추가|신규|기능", re.IGNORECASE)
FIX_RX = re.compile(r"^(fix|bugfix|hotfix|bug)\b|수정|오류|버그|고침", re.IGNORECASE)
PREFIX_RX = re.compile(r"^\s*(\[[^\]]*\]\s*)?([a-z]+)(\([^)]*\))?!?:\s*", re.IGNORECASE)
KIND_ORDER = ["추가", "수정", "변경"]


def clean_title(title, key=None):
    """제목 앞의 이슈 키·대괄호를 떼어 낸다(키는 따로 보여 주므로)."""
    t = (title or "").strip().splitlines()[0] if title else ""
    if key:
        # 뒤에 이어 붙인 키 목록(·3091·3088, , PILLDOC-2)까지 뗀다 — 구분 기호가 있을 때만
        more = r"(?:\s*[·,/&+]\s*(?:[A-Z][A-Z0-9]*-)?\d+)*"
        t = re.sub(rf"^\s*\[?{re.escape(key)}{more}\]?\s*[:\-]?\s*", "", t, flags=re.IGNORECASE)
    return t.strip() or "(제목 없음)"


def kind_of(title):
    """제목으로 추가·수정·변경을 가른다(conventional 접두어 우선, 다음 한글 낱말)."""
    t = title or ""
    m = PREFIX_RX.match(t)
    head = (m.group(2) if m else "") or ""
    if ADD_RX.match(head):
        return "추가"
    if FIX_RX.match(head):
        return "수정"
    body = t[m.end():] if m else t
    if FIX_RX.search(body):
        return "수정"
    if ADD_RX.search(body):
        return "추가"
    return "변경"


def strip_prefix(title):
    m = PREFIX_RX.match(title or "")
    return (title[m.end():] if m else title).strip() or title


def snapshot(repo):
    """state.json 의 레포 하나 → 지금 스테이징에만 있는 것의 요약(swap-log 에 그대로 남긴다)."""
    so = (repo.get("staging_only") or {}).get("commits") or []
    titles = {i["key"]: i.get("title") for i in repo.get("issues") or []}
    items, seen, nokey = [], set(), []
    for c in so:
        if not c.get("keys"):
            t = clean_title(c.get("message"))
            nokey.append({"sha": c["sha"], "title": strip_prefix(t), "kind": kind_of(t)})
            continue
        for k in c["keys"]:
            if k in seen:
                continue
            seen.add(k)
            t = clean_title(titles.get(k) or c.get("message"), k)
            items.append({"key": k, "title": clean_title(strip_prefix(t), k), "kind": kind_of(t)})
    return {"repo": repo.get("name"), "label": repo.get("label") or repo.get("name"), "app": repo.get("app"),
            "url": repo.get("url"),
            "prod_sha": (repo.get("prod") or {}).get("sha"), "staging_sha": (repo.get("staging") or {}).get("sha"),
            "commits": len(so), "items": items, "nokey": nokey,
            "known": repo.get("staging_only") is not None}


def _count(snap):
    n, m = len(snap["items"]), len(snap["nokey"])
    parts = []
    if n:
        parts.append(f"이슈 {n}건")
    if m:
        parts.append(f"기타 {m}건")
    return ", ".join(parts) or "커밋 0건"


def _rows(snap):
    return [(i["kind"], i["key"], i["title"]) for i in snap["items"]] + [(c["kind"], None, c["title"]) for c in snap["nokey"]]


def _bullets(rows):
    """rows: [(kind, key|None, title)] → 종류 순 불릿 줄(MAX_ITEMS 넘으면 「외 N건」).
    키는 글자 그대로 둔다 — 봇이 보낼 때 slack.post 가 Jira 링크로 바꾼다(코드로 감싸면 링크가 안 된다)."""
    rows = sorted(rows, key=lambda r: KIND_ORDER.index(r[0]) if r[0] in KIND_ORDER else len(KIND_ORDER))
    shown = rows if len(rows) <= MAX_ITEMS else rows[:MAX_ITEMS - 1]
    out = []
    for kind, key, title in shown:
        out.append([f"• {kind} " + (f"{key} " if key else ""), title])
    if len(shown) < len(rows):
        out.append([f"• 외 {len(rows) - len(shown)}건", ""])
    return out


def _fit(head_lines, bullets, tail_lines=()):
    """머리 줄 + 불릿 + 꼬리 줄을 MAX_CHARS 안에 맞춘다 — 불릿 제목만 고르게 줄인다."""
    def build(limit):
        rows = [p + (t if len(t) <= limit else t[:max(limit - 1, 1)] + "…") for p, t in bullets]
        return "\n".join(list(head_lines) + rows + list(tail_lines))
    limit = max((len(t) for _, t in bullets), default=0)
    text = build(limit)
    while len(text) > MAX_CHARS and limit > 8:
        limit -= 2
        text = build(limit)
    return text


def preview_text(snap):
    """배포 예고 문구. 나갈 것이 없거나 셀 수 없으면 None 과 이유."""
    if not snap["known"]:
        return None, "운영 SHA 를 몰라 나갈 커밋을 셀 수 없음"
    if not snap["commits"]:
        return None, "스테이징에만 있는 커밋 없음(나갈 것 없음)"
    head = [f"*{snap['label']} 운영 swap 시작 — {_count(snap)}*"]
    foot = " · ".join(p for p in (snap.get("url"), f"{snap['app']} staging → production", "결과는 이 글 스레드에") if p)
    return _fit(head, _bullets(_rows(snap)), [f"_{foot}_"]), None


def kst(iso):
    t = _iso(iso)
    return t.astimezone(KST).strftime("%m-%d %H:%M") if t else (iso or "?")


def result_text(rec):
    """swap-log 기록 하나 → 결과 문구. 스냅숏이 없는(이 기능 전) 기록이면 None 과 이유."""
    if not rec:
        return None, "이 PC 에서 이 레포를 swap 한 기록 없음"
    snap = (rec.get("info") or {}).get("snapshot") or {}
    label = snap.get("label") or rec.get("repo")
    when = kst(rec.get("finished_at"))
    check = [f"확인: {snap['url']}"] if snap.get("url") else []
    later = "확인하고 다시 알려 드리겠습니다."
    if not rec.get("ok") and rec.get("action") == "undo":
        return f"*{label} swap 되돌리기 실패 — 새 버전으로 운영 중 ({when})*\n{later}", None
    if not rec.get("ok"):
        return f"*{label} 운영 swap 실패 — 운영은 이전 버전 그대로 ({when})*\n{later}", None
    if rec.get("action") == "undo":
        return "\n".join([f"*{label} swap 되돌림 — 이전 버전으로 운영 중 ({when})*"] + check), None
    if "items" not in snap:
        return None, "이 swap 기록에는 나간 커밋 목록이 없음(알림 문구 기능 전 기록)"
    return _fit([f"*{label} 운영 swap 완료 ({when})*"] + check, _bullets(_rows(snap))), None


KEY_RX = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
SAME_SWAP_S = 300   # 보드가 한 swap 도 Azure 활동 기록에 남는다 — 보드 기록보다 이만큼 넘게 늦어야 다른 swap


def _iso(s):
    """ISO 시각 → aware datetime. Azure 의 7자리 소수·Z 도 받는다."""
    s = re.sub(r"(\.\d{6})\d+", r"\1", (s or "").replace("Z", "+00:00"))
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def outside_swap(repo, rec):
    """보드 밖(Azure Portal 등)에서 한 swap 의 결과 초안 → {text, at, note} 또는 None(보드 기록이 최신·swap 기록 없음).
    나간 커밋 목록은 모르므로 운영 SHA 를 배포한 run 의 제목으로 불릿을 만들고, 사람이 고쳐 보내게 한다."""
    swaps = repo.get("swaps") or []
    if not swaps:
        return None
    last = swaps[0]
    at, board = _iso(last.get("time")), _iso((rec or {}).get("finished_at"))
    if board and at and (at - board).total_seconds() <= SAME_SWAP_S:
        return None
    sha = (repo.get("prod") or {}).get("sha")
    run = next((x for x in repo.get("runs") or [] if sha and x.get("headSha") == sha), None)
    commits = [{"sha": sha, "message": run.get("displayTitle"), "keys": sorted(set(KEY_RX.findall(run.get("displayTitle") or "")))}] if run else []
    snap = snapshot(dict(repo, staging_only={"commits": commits}))
    label = snap["label"]
    check = [f"확인: {snap['url']}"] if snap.get("url") else []
    text = _fit([f"*{label} 운영 swap 완료 ({kst(last.get('time'))})*"] + check, _bullets(_rows(snap)))
    note = "보드 밖(Azure Portal 등)에서 한 swap — " + ("나간 커밋 목록은 보드가 모름, 운영 배포 run 제목으로 만든 초안이라 고쳐서 보내세요"
                                                   if run else "나간 커밋을 찾지 못함, 직접 채워서 보내세요")
    return {"text": text, "at": last.get("time"), "note": note}


def last_record(log_path, repo):
    """swap-log 에서 그 레포의 마지막 기록."""
    if not os.path.exists(log_path):
        return None
    last = None
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("repo") == repo:
                last = rec
    return last
