"""Slack 봇 전송(내 PC 모드 `--allow-swap` 에서만) — 환경 변수 두 개로 켠다.

- DEPLOY_BOARD_SLACK_TOKEN: 봇 토큰(xoxb-…, 범위 chat:write). 화면·로그·swap-log·state.json 어디에도 내보내지 않는다.
- DEPLOY_BOARD_SLACK_CHANNEL: 받을 채널 ID(C…/G…). 봇을 그 채널에 초대해 둔다.
둘 중 하나라도 없으면 꺼진다(보드는 「알림 문구」 복사만). 시험할 때는 DEPLOY_BOARD_SLACK_API 로 API 주소를 가짜 서버로 바꾼다.
"""
import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_API = "https://slack.com/api"
MAX_TEXT = 4000
CHANNEL_RX = re.compile(r"^[CG][A-Z0-9]{6,}$")


def load_env_file(path, env=None):
    """KEY=VALUE 파일(.env)에서 DEPLOY_BOARD_ 로 시작하는 키만 읽어 env 에 넣는다. 이미 있는 값은 덮지 않는다.
    → 넣은 키 목록(값은 돌려주지 않는다). 파일이 없으면 [] ."""
    env = os.environ if env is None else env
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    keys = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = (s.strip() for s in line.split("=", 1))
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k.startswith("DEPLOY_BOARD_") and v and k not in env:
            env[k] = v
            keys.append(k)
    return keys


def config(env=None, links=None):
    """→ {"token","channel","api"[,"jira_base","key_re"]} 또는 None(꺼짐)과 이유.
    links: repos.json 설정 — jira_base·issue_key_pattern 이 있으면 보낼 때 이슈 키를 Jira 링크로 바꾼다."""
    env = os.environ if env is None else env
    token = (env.get("DEPLOY_BOARD_SLACK_TOKEN") or "").strip()
    channel = (env.get("DEPLOY_BOARD_SLACK_CHANNEL") or "").strip()
    if not token or not channel:
        return None, "환경 변수 DEPLOY_BOARD_SLACK_TOKEN·DEPLOY_BOARD_SLACK_CHANNEL 이 없음"
    if not token.startswith("xoxb-"):
        return None, "DEPLOY_BOARD_SLACK_TOKEN 이 봇 토큰(xoxb-)이 아님"
    if not CHANNEL_RX.match(channel):
        return None, "DEPLOY_BOARD_SLACK_CHANNEL 이 채널 ID(C…) 모양이 아님"
    api = (env.get("DEPLOY_BOARD_SLACK_API") or DEFAULT_API).rstrip("/")
    if not (api.startswith("https://") or re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", api)):
        return None, "DEPLOY_BOARD_SLACK_API 는 https:// 주소만(시험용 127.0.0.1 제외)"
    cfg = {"token": token, "channel": channel, "api": api}
    links = links or {}
    if links.get("jira_base") and links.get("issue_key_pattern"):
        cfg["jira_base"] = links["jira_base"]
        cfg["key_re"] = re.compile(links["issue_key_pattern"])
    return cfg, None


def public(cfg, why=None):
    """화면에 보여 줄 상태(토큰 없음)."""
    return {"enabled": True, "channel": cfg["channel"]} if cfg else {"enabled": False, "reason": why}


def escape(text):
    """Slack 제어 문자(&, <, >)를 글자로 — 커밋 제목의 <!channel>·<url|글> 이 멘션·링크가 되지 않게."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def linkify(escaped, cfg):
    """escape 한 글의 이슈 키 → <jira_base+키|키>. 키 규칙(repos.json)에 맞는 것만 — 「UTF-8」 같은 글자는 그대로."""
    if not cfg.get("key_re") or not cfg.get("jira_base"):
        return escaped
    return cfg["key_re"].sub(lambda m: f"<{cfg['jira_base']}{m.group(0)}|{m.group(0)}>", escaped)


def _scrub(msg, cfg):
    return str(msg).replace(cfg["token"], "***")[:300]


def post(cfg, text, thread_ts=None, timeout=10):
    """chat.postMessage → (ok, ts, error). 예외를 밖으로 던지지 않는다."""
    if not text or not text.strip():
        return False, None, "보낼 문구가 비어 있음"
    out = linkify(escape(text), cfg)
    if len(out) > MAX_TEXT:
        return False, None, f"문구가 {MAX_TEXT}자를 넘음"
    body = {"channel": cfg["channel"], "text": out, "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        body["thread_ts"] = thread_ts
    req = urllib.request.Request(cfg["api"] + "/chat.postMessage", data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "Authorization": "Bearer " + cfg["token"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return False, None, _scrub(f"HTTP {e.code}", cfg)
    except Exception as e:
        return False, None, _scrub(f"전송 실패 — {e}", cfg)
    if not d.get("ok"):
        return False, None, _scrub(f"Slack 오류 — {d.get('error') or '알 수 없음'}", cfg)
    if not d.get("ts"):
        return False, None, "Slack 응답에 글 ts 가 없음"
    return True, d.get("ts"), None
