"""배포 알림 문구(Slack mrkdwn) — 보드는 어디에도 보내지 않고 복사할 문구만 만든다.

- 예고: 지금 스테이징에만 있는 커밋(= swap 하면 운영에 나갈 것)을 이슈 키별로 묶는다.
- 결과: swap 할 때 swap-log 에 남긴 스냅숏(그 순간 나간 것)으로 「반영됨」 문구를 만든다.
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
        t = re.sub(rf"^\s*\[?{re.escape(key)}\]?\s*[:\-]?\s*", "", t, flags=re.IGNORECASE)
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
            "prod_sha": (repo.get("prod") or {}).get("sha"), "staging_sha": (repo.get("staging") or {}).get("sha"),
            "commits": len(so), "items": items, "nokey": nokey,
            "known": repo.get("staging_only") is not None}


def _count(snap):
    n, m = len(snap["items"]), len(snap["nokey"])
    parts = []
    if n:
        parts.append(f"이슈 {n}건")
    if m:
        parts.append(f"키 없는 커밋 {m}건")
    return " + ".join(parts) or "커밋 0건"


def _bullets(rows, with_kind=False):
    """rows: [(kind, key|None, title)] → 불릿 줄(MAX_ITEMS 넘으면 「외 N건」)."""
    if with_kind:
        rows = sorted(rows, key=lambda r: KIND_ORDER.index(r[0]))
    shown = rows if len(rows) <= MAX_ITEMS else rows[:MAX_ITEMS - 1]
    out = []
    for kind, key, title in shown:
        head = (f"{kind} " if with_kind else "") + (f"`{key}` " if key else "")
        out.append([f"• {head}", title])
    if len(shown) < len(rows):
        out.append([f"• 외 {len(rows) - len(shown)}건", ""])
    return out


def _fit(head_lines, bullets):
    """머리 줄 + 불릿을 MAX_CHARS 안에 맞춘다 — 불릿 제목을 고르게 줄인다."""
    def build(limit):
        rows = [p + (t if len(t) <= limit else t[:max(limit - 1, 1)] + "…") for p, t in bullets]
        return "\n".join(head_lines + rows)
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
    head = [f"*{snap['label']} 운영 반영 예정 — {_count(snap)}*",
            f"대상: `{snap['app']}` staging → production"]
    rows = [(i["kind"], i["key"], i["title"]) for i in snap["items"]] + [(c["kind"], None, c["title"]) for c in snap["nokey"]]
    return _fit(head, _bullets(rows)), None


def kst(iso):
    try:
        return dt.datetime.fromisoformat(iso).astimezone(KST).strftime("%m-%d %H:%M")
    except Exception:
        return iso or "?"


def result_text(rec):
    """swap-log 기록 하나 → 결과 문구. 스냅숏이 없는(이 기능 전) 기록이면 None 과 이유."""
    if not rec:
        return None, "이 PC 에서 이 레포를 swap 한 기록 없음"
    label = (rec.get("info") or {}).get("snapshot", {}).get("label") or rec.get("repo")
    when = kst(rec.get("finished_at"))
    if not rec.get("ok"):
        return f"*{label} 운영 반영 실패 — 운영은 그대로*\n시각: {when}\n원인 확인 중입니다.", None
    if rec.get("action") == "undo":
        return f"*{label} 운영 반영 되돌림 — 이전 운영으로 복구*\n시각: {when}", None
    snap = (rec.get("info") or {}).get("snapshot")
    if not snap:
        return None, "이 swap 기록에는 나간 커밋 목록이 없음(알림 문구 기능 전 기록)"
    rows = [(i["kind"], i["key"], i["title"]) for i in snap["items"]] + [(c["kind"], None, c["title"]) for c in snap["nokey"]]
    kinds = {k: sum(1 for r in rows if r[0] == k) for k in KIND_ORDER}
    summary = " · ".join(f"{k} {n}" for k, n in kinds.items() if n) or "변경 없음"
    head = [f"*{label} 운영 반영 완료 — {_count(snap)}*", f"시각: {when} · {summary}"]
    return _fit(head, _bullets(rows, with_kind=True)), None


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
