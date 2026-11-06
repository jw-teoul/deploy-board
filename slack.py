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


def config(env=None):
    """→ {"token","channel","api"} 또는 None(꺼짐)과 이유."""
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
    return {"token": token, "channel": channel, "api": api}, None


def public(cfg, why=None):
    """화면에 보여 줄 상태(토큰 없음)."""
    return {"enabled": True, "channel": cfg["channel"]} if cfg else {"enabled": False, "reason": why}


def escape(text):
    """Slack 제어 문자(&, <, >)를 글자로 — 커밋 제목의 <!channel>·<url|글> 이 멘션·링크가 되지 않게."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _scrub(msg, cfg):
    return str(msg).replace(cfg["token"], "***")[:300]


def post(cfg, text, thread_ts=None, timeout=10):
    """chat.postMessage → (ok, ts, error). 예외를 밖으로 던지지 않는다."""
    if not text or not text.strip():
        return False, None, "보낼 문구가 비어 있음"
    if len(escape(text)) > MAX_TEXT:
        return False, None, f"문구가 {MAX_TEXT}자를 넘음"
    body = {"channel": cfg["channel"], "text": escape(text), "unfurl_links": False, "unfurl_media": False}
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
