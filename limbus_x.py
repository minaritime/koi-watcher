"""림버스 컴퍼니 공식 X(@LimbusCompany_B) → 디스코드 알림. 스팀 알리미의 덤.

스팀에 안 올라오는 소식(인격 예고 티저 이미지, 캐릭터 생일 이미지, 가끔 빠지는
공지)만 보낸다. limbus_watcher.py 가 스팀 처리 뒤에 process_x() 를 부른다.

- 읽기: X 공식 API 는 유료라 비로그인 우회 두 곳(fxtwitter API, Nitter RSS)을
  둘 다 읽어 합친다. 2026-10-25 시험에서 GitHub IP 기준 각각 4/6, 5/6 성공,
  둘 중 하나는 매번 성공.
- 거르기: 리트윗(음원·콘서트·굿즈 대부분)과 답글은 빼고, 비게임 키워드도 뺀다.
  제목이 같은 스팀 공지가 있으면 뺀다 (겹치는 공지는 늘 스팀이 먼저 올라옴).
  기준점(limbus_x_seen.json 의 since) 이전 글은 절대 보내지 않는다.
- 끊김: 두 곳 다 실패하면 바로 디스코드에 알림(@everyone 없이 지정한 사람만
  멘션), 다시 연결되면 복구 알림. X 실패는 워크플로우 실패(메일)로 치지 않는다.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import requests

KST = timezone(timedelta(hours=9))
SCRIPT_DIR = Path(__file__).parent
X_SEEN_PATH = SCRIPT_DIR / "limbus_x_seen.json"
X_STATE_PATH = SCRIPT_DIR / "limbus_x_state.json"

FXTWITTER_API = "https://api.fxtwitter.com/2/profile/{handle}/statuses"
FETCH_ATTEMPTS = 3            # Nitter 는 503 이 잦아 한 실행 안에서 몇 번 더 시도
FETCH_RETRY_WAIT_SECONDS = 5
STEAM_GRACE_SECONDS = 15 * 60  # 제목 있는 글은 스팀에 올라올 시간을 주고 판단
MAX_AGE_SECONDS = 7 * 86400    # 이보다 오래된 글은 보내지 않고 본 것으로만 기록
X_CONTENT_HEADER = "@everyone 📢 림버스 컴퍼니 X 새 소식이 올라왔습니다"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}

# 리트윗을 빼도 계정이 직접 올리는 비게임 소식을 한 번 더 거른다.
# 영어는 단어 단위로만 ("Mili" 가 "familiar" 에 걸리지 않게), 한·일은 포함 여부로.
NON_GAME_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:Mili|MILI|concert|Concert|CONCERT|album|Album|"
    r"Tokyo Game Show|TGS)(?![A-Za-z])"
    r"|콘서트|コンサート|음원|앨범|アルバム|굿즈|グッズ|팝업|ポップアップ|페어|フェア"
    r"|코믹마켓|コミックマーケット|ねんどろいど|ぬいぐるみ"
)
SAME_DAY_SECONDS = 24 * 3600   # 숫자만 다른 제목은 이 시간 안의 스팀 공지일 때만 같은 공지


# ---------- 읽기 ----------

def _tweet(tid: str, ts: int, text: str, photos: list[str], has_video: bool,
           is_retweet: bool, is_reply: bool) -> dict:
    return {"id": tid, "ts": ts, "text": text, "photos": photos, "has_video": has_video,
            "is_retweet": is_retweet, "is_reply": is_reply}


def _orig(url: str) -> str:
    return url if "name=" in url else url + ("&" if "?" in url else "?") + "name=orig"


def fetch_fxtwitter(handle: str, timeout: int) -> list[dict]:
    resp = requests.get(FXTWITTER_API.format(handle=handle), headers=_UA, timeout=timeout)
    resp.raise_for_status()
    tweets = []
    for t in resp.json().get("results") or []:
        media = t.get("media") or {}
        tweets.append(_tweet(
            str(t["id"]), int(t["created_timestamp"]), t.get("text") or "",
            [_orig(p["url"]) for p in media.get("photos") or []],
            bool(media.get("videos")),
            bool(t.get("reposted_by")), bool(t.get("replying_to")),
        ))
    return tweets


def fetch_nitter(base: str, handle: str, timeout: int) -> list[dict]:
    resp = requests.get(f"{base}/{handle}/rss", headers=_UA, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    tweets = []
    for item in re.findall(r"<item>(.*?)</item>", resp.text, re.S):
        m_title = re.search(r"<title>(.*?)</title>", item, re.S)
        m_id = re.search(r"<guid[^>]*>(\d+)</guid>", item)
        m_date = re.search(r"<pubDate>(.*?)</pubDate>", item)
        if not (m_title and m_id and m_date):
            continue  # 깨진 항목 하나 때문에 나머지를 버리지 않는다
        title = html.unescape(m_title.group(1))
        tid = m_id.group(1)
        ts = int(parsedate_to_datetime(m_date.group(1)).timestamp())
        desc = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>", item, re.S)
        desc = desc.group(1) if desc else ""
        imgs = re.findall(r'<img src="([^"]+)"', desc)
        text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<img[^>]*>", "", desc))).strip()
        tweets.append(_tweet(
            tid, ts, text,
            [_orig(u) for u in imgs if "/media/" in u],
            any("video_thumb" in u for u in imgs),
            title.startswith("RT by @"), title.startswith("R to @"),
        ))
    return tweets


def _with_retry(fn: Callable[[], list[dict]]) -> list[dict]:
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            items = fn()
            if items:
                return items
            last = RuntimeError("0건")
        except Exception as e:
            last = e
        if attempt < FETCH_ATTEMPTS - 1:
            time.sleep(FETCH_RETRY_WAIT_SECONDS)
    raise last or RuntimeError("0건")


def fetch_tweets(config: dict, log: logging.Logger) -> list[dict]:
    """모든 경로를 읽어 id 기준으로 합친다. 전부 실패하면 빈 목록."""
    handle = config["x_handle"]
    timeout = config.get("request_timeout_seconds", 15)
    sources: list[tuple[str, Callable[[], list[dict]]]] = [
        ("fxtwitter", lambda: fetch_fxtwitter(handle, timeout)),
    ]
    for base in config.get("x_nitter_instances", []):
        sources.append((base, lambda b=base: fetch_nitter(b, handle, timeout)))

    merged: dict[str, dict] = {}
    for name, fn in sources:
        try:
            items = _with_retry(fn)
        except Exception as e:
            log.warning("X 경로 %s 실패: %s", name, e)
            continue
        log.info("X 경로 %s: %d건", name, len(items))
        for t in items:
            merged.setdefault(t["id"], t)  # 먼저 읽은 fxtwitter 정보를 우선
    return sorted(merged.values(), key=lambda t: t["ts"])


# ---------- 거르기 ----------

def headline(text: str) -> str:
    """공지 제목. '[제목] [日本語] [English]' 또는 '제목 / 日本語 / English' 형식."""
    s = text.strip()
    m = re.match(r"\[([^\]]+)\]", s)
    if m and not re.fullmatch(r"\d{2}-\d{2}", m.group(1)):
        return m.group(1).strip()
    first = s.split("\n")[0]
    if " /" in first or "/ " in first:
        return re.split(r"\s*/", first)[0].strip()
    return ""


def display_title(text: str) -> str:
    """디스코드에 보일 제목. 해시태그·링크만 있는 티저 이미지는 기본 문구."""
    title = headline(text)
    if title:
        return title
    rest = re.sub(r"#\S+|https?://\S+", "", text)
    rest = " ".join(rest.split())
    return rest[:100] or "새 이미지 게시물"


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s).lower()


def is_on_steam(title: str, ts: int, steam_notices: list[dict]) -> bool:
    """정규화한 제목이 같으면 같은 공지. 숫자만 다른 경우는 같은 날 올라온
    스팀 공지일 때만 같다고 본다 (스팀 제목 날짜 오타 사례: X '10.29' /
    스팀 '10.22', 2026-10-23). 지난주 '…추가 안내' 와 이번 주 것을 섞지 않기 위함."""
    n = _norm(title)
    if not n:
        return False
    digitless = re.sub(r"\d", "#", n)
    for sn in steam_notices:
        s = _norm(sn["title"])
        if s == n:
            return True
        same_day = abs(int(sn.get("posttime") or 0) - ts) <= SAME_DAY_SECONDS
        if same_day and re.sub(r"\d", "#", s) == digitless:
            return True
    return False


def classify(t: dict, steam_notices: list[dict], now: int, since: int) -> str:
    """'send' | 'skip'(본 것으로 기록) | 'wait'(다음 실행에 다시 판단)."""
    if t["is_retweet"] or t["is_reply"]:
        return "skip"
    if NON_GAME_PATTERN.search(t["text"]):
        return "skip"
    # 기준점 이전 글은 절대 보내지 않는다 (기준점이 한쪽 경로로만 잡혔을 때
    # 다른 경로가 옛 글을 들고 와도 @everyone 재발송이 없게)
    if t["ts"] < since or now - t["ts"] > MAX_AGE_SECONDS:
        return "skip"
    title = headline(t["text"])
    if not title and not t["photos"] and not t["has_video"]:
        return "skip"  # 이모지 한 줄 같은 글
    if title:
        if is_on_steam(title, t["ts"], steam_notices):
            return "skip"
        if now - t["ts"] < STEAM_GRACE_SECONDS:
            return "wait"
    return "send"


def to_notice(t: dict, handle: str) -> dict:
    """limbus_watcher.send_discord 가 받는 공지 형식으로 변환."""
    url = f"https://x.com/{handle}/status/{t['id']}"
    text = re.sub(r"https?://t\.co/\S+", "", t["text"]).strip()
    return {
        "id": t["id"],
        "title": display_title(t["text"])[:250],
        "date": datetime.fromtimestamp(t["ts"], tz=KST).strftime("%Y-%m-%d %H:%M"),
        "posttime": t["ts"],
        "url": url,
        "images": t["photos"],
        # X 영상은 fxtwitter 링크여야 디스코드에서 재생 카드가 뜬다
        "video": f"https://fxtwitter.com/{handle}/status/{t['id']}" if t["has_video"] else None,
        "text": text,
        "source_label": "Limbus Company · X",
        "header": X_CONTENT_HEADER,
    }


# ---------- 상태 ----------

def _load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------- 끊김 알림 ----------

def _alert(webhook_url: str, config: dict, message: str) -> None:
    uid = str(config.get("x_alert_mention_user_id") or "").strip()
    payload = {
        "username": config.get("webhook_username", "림버스 공지 알리미"),
        "content": (f"<@{uid}> " if uid else "") + message,
        "allowed_mentions": {"parse": [], "users": [uid] if uid else []},
    }
    resp = requests.post(webhook_url, json=payload, timeout=30)
    resp.raise_for_status()


def update_link_state(ok: bool, webhook_url: str, config: dict, log: logging.Logger) -> None:
    """두 경로 다 실패하면 끊김 알림 1번, 회복하면 복구 알림 1번."""
    state = _load_json(X_STATE_PATH, {})
    now = int(time.time())
    try:
        if not ok and not state.get("alerted"):
            _alert(webhook_url, config,
                   "⚠️ 림버스 X 소식 연결이 끊겼습니다. fxtwitter와 Nitter 모두 응답이 없어서 "
                   "인격 예고 같은 X 소식을 받을 수 없습니다. 스팀 공지는 계속 옵니다. "
                   "다시 연결되면 알려 드립니다.")
            _save_json(X_STATE_PATH, {"down_since": state.get("down_since") or now, "alerted": True})
            log.warning("X 끊김 알림 전송")
        elif ok and state.get("alerted"):
            mins = (now - state.get("down_since", now)) // 60
            _alert(webhook_url, config,
                   f"✅ 림버스 X 소식 연결이 복구되었습니다. (끊겨 있던 시간: 약 {mins // 60}시간 {mins % 60}분) "
                   "그사이 올라온 소식이 있으면 이어서 보냅니다.")
            _save_json(X_STATE_PATH, {})
            log.info("X 복구 알림 전송")
    except Exception as e:
        log.error("X 끊김/복구 알림 전송 실패 (다음 실행에서 재시도): %s", e)


# ---------- 진입점 ----------

def load_x_seen() -> tuple[int, set[str]] | None:
    """{"since": 기준 시각, "ids": [...]}. 없거나 깨졌으면 None (다시 기준점 잡기)."""
    data = _load_json(X_SEEN_PATH, None)
    if not isinstance(data, dict) or not isinstance(data.get("since"), int) \
            or not isinstance(data.get("ids"), list):
        return None
    return data["since"], set(map(str, data["ids"]))


def save_x_seen(since: int, ids: set[str]) -> None:
    _save_json(X_SEEN_PATH, {"since": since, "ids": sorted(ids)})


def process_x(config: dict, webhook_url: str, steam_notices: list[dict],
              send: Callable[[dict], None], log: logging.Logger) -> None:
    """X 새 글을 걸러 send(notice) 로 보낸다. 어떤 실패도 밖으로 던지지 않는다
    (X 문제로 워크플로우가 실패해 메일이 가면 안 됨)."""
    try:
        _process_x(config, webhook_url, steam_notices, send, log)
    except Exception:
        log.exception("X 처리 중 예외 (스팀 알림에는 영향 없음)")


def _process_x(config: dict, webhook_url: str, steam_notices: list[dict],
               send: Callable[[dict], None], log: logging.Logger) -> None:
    tweets = fetch_tweets(config, log)
    update_link_state(bool(tweets), webhook_url, config, log)
    if not tweets:
        return

    now = int(time.time())
    loaded = load_x_seen()
    if loaded is None:
        save_x_seen(now, {t["id"] for t in tweets})
        log.info("X 첫 실행: %d건을 baseline 으로 기록 (알림 미발송)", len(tweets))
        return
    since, seen = loaded

    for t in tweets:  # 오래된 것부터
        if t["id"] in seen:
            continue
        verdict = classify(t, steam_notices, now, since)
        if verdict == "wait":
            continue
        if verdict == "send":
            notice = to_notice(t, config["x_handle"])
            try:
                send(notice)
                log.info("X 전송 완료: %s", notice["title"])
            except Exception as e:
                log.error("X 전송 실패 (%s): %s", notice["title"], e)
                continue  # seen 에 안 넣어 다음 실행에서 재시도
        seen.add(t["id"])
        save_x_seen(since, seen)
