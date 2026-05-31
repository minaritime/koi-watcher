"""림버스 컴퍼니(Limbus Company) 스팀 공지 → 디스코드 알림.

Steam News API(GetNewsForApp)로 림버스 컴퍼니(appid 1973530) 공식 공지를
폴링해서, 이전에 보지 못한 새 공지가 있으면 디스코드 웹훅으로 전송한다.

첫 실행 시에는 현재 보이는 모든 공지를 "이미 봤음" 상태로만 기록하고
알림은 보내지 않는다 (스팸 방지).

KOI 알리미(koi_watcher.py)와 동일한 구조이며, 차이는 HTML 파싱 대신
공식 JSON API 를 쓴다는 점뿐이다.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "limbus_config.json"
SECRETS_PATH = SCRIPT_DIR / "secrets.json"
SEEN_PATH = SCRIPT_DIR / "limbus_seen.json"
LOG_PATH = SCRIPT_DIR / "limbus_watcher.log"

# 스팀 스토어 이벤트 API. l=koreana 로 한국어 공지를 받을 수 있다.
# (ISteamNews API 는 영어만 제공해서 이 엔드포인트로 교체함)
STEAM_EVENTS_API = "https://store.steampowered.com/events/ajaxgetpartnereventspageable/"
STORE_VIEW_URL = "https://store.steampowered.com/news/app/{appid}/view/{gid}?l={lang}"

# 스팀 공지 본문의 {STEAM_CLAN_IMAGE} placeholder 를 실제 CDN 주소로 치환
STEAM_IMG_BASE = "https://clan.akamai.steamstatic.com/images"

_IMG_BBCODE = re.compile(r"\[img\]\s*([^\[\]]+?)\s*\[/img\]", re.I)
_IMG_HTML = re.compile(r"<img[^>]+src=[\"']?([^\"'> ]+)", re.I)
_YOUTUBE = re.compile(r"previewyoutube=([\w-]+)", re.I)


def _resolve_img(url: str) -> str | None:
    url = url.strip().replace("{STEAM_CLAN_IMAGE}", STEAM_IMG_BASE)
    return url if url.startswith("http") else None


def extract_media(contents: str) -> tuple[list[str], str | None]:
    """공지 본문에서 이미지 URL 목록과 (있으면) 유튜브 영상 URL 을 추출."""
    images: list[str] = []
    for raw in _IMG_BBCODE.findall(contents) + _IMG_HTML.findall(contents):
        resolved = _resolve_img(raw)
        if resolved and resolved not in images:
            images.append(resolved)
    yt = _YOUTUBE.search(contents)
    video = f"https://www.youtube.com/watch?v={yt.group(1)}" if yt else None
    return images, video


def clean_text(contents: str) -> str:
    """BBCode/HTML 을 제거해 사람이 읽을 수 있는 평문으로 변환."""
    t = contents
    t = re.sub(r"\[previewyoutube=[^\]]*\].*?\[/previewyoutube\]", "", t, flags=re.I | re.S)
    t = re.sub(r"\[img\].*?\[/img\]", "", t, flags=re.I | re.S)
    t = re.sub(r"\[/?[a-z][^\]]*\]", "", t, flags=re.I)   # 나머지 BBCode 태그 제거
    t = re.sub(r"<[^>]+>", "", t)                          # HTML 태그 제거
    t = html.unescape(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def setup_logging() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_webhook_url() -> str:
    """1순위: 환경변수 LIMBUS_WEBHOOK_URL, 2순위: secrets.json 의 limbus_webhook_url."""
    env = os.environ.get("LIMBUS_WEBHOOK_URL", "").strip()
    if env:
        return env
    if SECRETS_PATH.exists():
        with open(SECRETS_PATH, encoding="utf-8") as f:
            url = json.load(f).get("limbus_webhook_url", "").strip()
            if url:
                return url
    raise RuntimeError(
        "Discord webhook URL 을 찾지 못했습니다. "
        "환경변수 LIMBUS_WEBHOOK_URL 또는 secrets.json 의 limbus_webhook_url 을 설정하세요."
    )


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    with open(SEEN_PATH, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(seen: set[str]) -> None:
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def fetch_notices(config: dict) -> list[dict]:
    """Steam 스토어 이벤트 API(한국어)를 호출해 공지 목록을 반환."""
    resp = requests.get(
        STEAM_EVENTS_API,
        params={
            "clan_accountid": 0,
            "appid": config["appid"],
            "offset": 0,
            "count": config.get("news_count", 20),
            "l": config.get("language", "koreana"),
            "origin": "https://store.steampowered.com",
        },
        headers={"User-Agent": "Mozilla/5.0 (Limbus-Watcher)"},
        timeout=config.get("request_timeout_seconds", 15),
    )
    resp.raise_for_status()
    return parse_notices(resp.json(), config)


def parse_notices(data: dict, config: dict) -> list[dict]:
    """이벤트 API 응답을 [{id, title, date, url, images, video, text}, ...] 로 변환."""
    events = data.get("events", [])
    appid = config["appid"]
    lang = config.get("language", "koreana")

    notices: list[dict] = []
    for ev in events:
        gid = str(ev.get("gid", "")).strip()
        body_obj = ev.get("announcement_body", {}) or {}
        title = (body_obj.get("headline") or "").strip()
        if not gid or not title:
            continue
        body = body_obj.get("body", "") or ""
        posttime = body_obj.get("posttime") or ev.get("rtime32_start_time") or 0
        date_str = datetime.fromtimestamp(posttime, tz=KST).strftime("%Y-%m-%d")
        url = STORE_VIEW_URL.format(appid=appid, gid=gid, lang=lang)

        images, video = extract_media(body)
        # 이벤트의 영상 전용 필드가 있으면 우선 사용
        if ev.get("video_preview_type") == "youtube" and ev.get("video_preview_id"):
            video = f"https://www.youtube.com/watch?v={ev['video_preview_id']}"

        notices.append({
            "id": gid,
            "title": title,
            "date": date_str,
            "url": url,
            "images": images,
            "video": video,
            "text": clean_text(body),
        })

    return notices


CONTENT_HEADER = "@everyone 📢 림버스 컴퍼니 새 공지가 올라왔습니다"
EMBED_COLOR = 0xC0392B
MAX_IMAGES = 4          # 디스코드에 한 번에 보여줄 이미지 최대 개수
MAX_TEXT_LEN = 600      # 텍스트 공지 본문 발췌 길이


def build_payload(username: str, notice: dict) -> dict:
    """이미지>영상>텍스트 우선순위로 디스코드 페이로드를 만든다.

    - 이미지 있음: 이미지를 임베드로 표시 (여러 장이면 갤러리로 묶음)
    - 영상 있음: 유튜브 URL 을 content 에 넣어 디스코드 재생 카드로 표시
    - 둘 다 없음: 제목 + 본문 발췌 텍스트
    """
    title, date, link = notice["title"], notice["date"], notice["url"]
    images = notice.get("images") or []
    video = notice.get("video")
    base = {"username": username, "allowed_mentions": {"parse": ["everyone"]}}

    if images:
        embeds = []
        for i, img in enumerate(images[:MAX_IMAGES]):
            # 같은 url 을 공유하는 임베드들은 디스코드가 한 카드의 갤러리로 묶어 보여준다.
            embed = {"url": link, "image": {"url": img}, "color": EMBED_COLOR}
            if i == 0:
                embed["title"] = title
                embed["fields"] = [{"name": "게시일", "value": date, "inline": True}]
                embed["footer"] = {"text": "Limbus Company · Steam 공지"}
            embeds.append(embed)
        return {**base, "content": CONTENT_HEADER, "embeds": embeds}

    if video:
        # 유튜브 URL 을 content 에 그대로 두면 디스코드가 재생 가능한 카드로 자동 임베드한다.
        return {
            **base,
            "content": f"{CONTENT_HEADER}\n**{title}**  (게시일 {date})\n{video}",
        }

    # 텍스트만 있는 공지
    text = notice.get("text") or ""
    desc = text[:MAX_TEXT_LEN] + ("…" if len(text) > MAX_TEXT_LEN else "")
    return {
        **base,
        "content": CONTENT_HEADER,
        "embeds": [{
            "title": title,
            "url": link,
            "color": EMBED_COLOR,
            "description": desc or "(본문 없음)",
            "fields": [{"name": "게시일", "value": date, "inline": True}],
            "footer": {"text": "Limbus Company · Steam 공지"},
        }],
    }


def send_discord(webhook_url: str, username: str, notice: dict) -> None:
    payload = build_payload(username, notice)
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def main() -> int:
    setup_logging()
    log = logging.getLogger("limbus_watcher")

    try:
        config = load_config()
        webhook_url = load_webhook_url()
    except Exception as e:
        log.error("설정 로드 실패: %s", e)
        return 2

    try:
        notices = fetch_notices(config)
    except Exception as e:
        log.error("Steam 뉴스 fetch 실패: %s", e)
        return 1

    if not notices:
        log.warning("공지가 0건 파싱됨 — API 응답 또는 feed_filter 를 확인하세요")
        return 1

    log.info("총 %d건 파싱됨", len(notices))

    seen = load_seen()
    first_run = not SEEN_PATH.exists()

    if first_run:
        for n in notices:
            seen.add(n["id"])
        save_seen(seen)
        log.info("첫 실행: %d건을 baseline 으로 기록 (알림 미발송)", len(notices))
        return 0

    new_items = [n for n in notices if n["id"] not in seen]
    if not new_items:
        log.info("새 공지 없음")
        return 0

    log.info("새 공지 %d건 발견 — 디스코드 전송 시작", len(new_items))
    # API 는 최신순이므로 오래된 것부터 보내도록 reverse
    for notice in reversed(new_items):
        try:
            send_discord(
                webhook_url,
                config.get("webhook_username", "림버스 공지 알리미"),
                notice,
            )
            seen.add(notice["id"])
            save_seen(seen)
            log.info("전송 완료: %s", notice["title"])
        except Exception as e:
            log.error("전송 실패 (%s): %s", notice["title"], e)
            # 실패 시 seen 에 추가하지 않음 → 다음 실행에서 재시도

    return 0


if __name__ == "__main__":
    sys.exit(main())
