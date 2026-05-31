"""림버스 컴퍼니(Limbus Company) 스팀 공지 → 디스코드 알림.

Steam News API(GetNewsForApp)로 림버스 컴퍼니(appid 1973530) 공식 공지를
폴링해서, 이전에 보지 못한 새 공지가 있으면 디스코드 웹훅으로 전송한다.

첫 실행 시에는 현재 보이는 모든 공지를 "이미 봤음" 상태로만 기록하고
알림은 보내지 않는다 (스팸 방지).

KOI 알리미(koi_watcher.py)와 동일한 구조이며, 차이는 HTML 파싱 대신
공식 JSON API 를 쓴다는 점뿐이다.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "limbus_config.json"
SECRETS_PATH = SCRIPT_DIR / "secrets.json"
SEEN_PATH = SCRIPT_DIR / "limbus_seen.json"
LOG_PATH = SCRIPT_DIR / "limbus_watcher.log"

STEAM_NEWS_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"


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
    """Steam News API 를 호출해 공지 목록을 [{id, title, date, url}, ...] 로 반환."""
    resp = requests.get(
        STEAM_NEWS_API,
        params={
            "appid": config["appid"],
            "count": config.get("news_count", 20),
            "format": "json",
        },
        headers={"User-Agent": "Mozilla/5.0 (Limbus-Watcher)"},
        timeout=config.get("request_timeout_seconds", 15),
    )
    resp.raise_for_status()
    return parse_notices(resp.json(), config)


def parse_notices(data: dict, config: dict) -> list[dict]:
    """API 응답을 파싱. feed_filter 가 설정되면 해당 피드(공식 공지)만 남긴다."""
    items = data.get("appnews", {}).get("newsitems", [])
    feed_filter = config.get("feed_filter")
    view_base = config.get("store_view_url", "")

    notices: list[dict] = []
    for it in items:
        if feed_filter and it.get("feedname") != feed_filter:
            continue
        gid = str(it.get("gid", "")).strip()
        title = (it.get("title") or "").strip()
        if not gid or not title:
            continue
        # gid 로 스팀 뉴스 보기 링크를 만들고, 실패 시 API 가 준 url 로 폴백
        url = (view_base + gid) if view_base else (it.get("url") or "")
        date_str = datetime.fromtimestamp(
            it.get("date", 0), tz=timezone.utc
        ).strftime("%Y-%m-%d")
        notices.append({
            "id": gid,
            "title": title,
            "date": date_str,
            "url": url,
        })

    return notices


def send_discord(webhook_url: str, username: str, notice: dict) -> None:
    payload = {
        "username": username,
        "content": "@everyone 📢 림버스 컴퍼니 새 공지가 올라왔습니다",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [
            {
                "title": notice["title"],
                "url": notice["url"],
                "color": 0xC0392B,
                "fields": [
                    {"name": "게시일", "value": notice["date"], "inline": True},
                ],
                "footer": {"text": "Limbus Company · Steam 공지"},
            }
        ],
    }
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
