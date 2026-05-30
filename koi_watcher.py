"""KOI(한국정보올림피아드) 공지사항 모니터링 → 디스코드 알림.

매 실행 시 https://koi.or.kr/notice 를 fetch 해서 이전에 보지 못한
새 공지가 있으면 디스코드 웹훅으로 알림을 전송한다.

첫 실행 시에는 현재 보이는 모든 공지를 "이미 봤음" 상태로만 기록하고
알림은 보내지 않는다 (스팸 방지).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SECRETS_PATH = SCRIPT_DIR / "secrets.json"
SEEN_PATH = SCRIPT_DIR / "seen.json"
LOG_PATH = SCRIPT_DIR / "koi_watcher.log"


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
    """1순위: 환경변수 DISCORD_WEBHOOK_URL, 2순위: secrets.json."""
    env = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if env:
        return env
    if SECRETS_PATH.exists():
        with open(SECRETS_PATH, encoding="utf-8") as f:
            url = json.load(f).get("discord_webhook_url", "").strip()
            if url:
                return url
    raise RuntimeError(
        "Discord webhook URL 을 찾지 못했습니다. "
        "환경변수 DISCORD_WEBHOOK_URL 또는 secrets.json 을 설정하세요."
    )


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    with open(SEEN_PATH, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(seen: set[str]) -> None:
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def fetch_notices(url: str, timeout: int) -> list[dict]:
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (KOI-Watcher)"},
        timeout=timeout,
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return parse_notices(resp.text)


def parse_notices(html: str) -> list[dict]:
    """공지 표를 파싱해 [{id, title, date, href}, ...] 로 반환."""
    soup = BeautifulSoup(html, "html.parser")
    notices: list[dict] = []

    for row in soup.select("tr"):
        link = row.select_one("td.title-cell a.u-url")
        date_cell = row.select_one("td.date-cell")
        if not link or not date_cell:
            continue
        href = link.get("href", "").strip()
        title = link.get_text(strip=True)
        date = date_cell.get_text(strip=True)
        if not href or not title:
            continue
        notices.append({
            "id": href,
            "title": title,
            "date": date,
            "href": href,
        })

    return notices


def send_discord(webhook_url: str, username: str, notice: dict, base_url: str) -> None:
    full_url = urljoin(base_url + "/", notice["href"].lstrip("/"))
    payload = {
        "username": username,
        "content": "@everyone 📢 KOI 새 공지가 올라왔습니다",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [
            {
                "title": notice["title"],
                "url": full_url,
                "color": 0x2E86DE,
                "fields": [
                    {"name": "게시일", "value": notice["date"], "inline": True},
                ],
                "footer": {"text": "koi.or.kr/notice"},
            }
        ],
    }
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def main() -> int:
    setup_logging()
    log = logging.getLogger("koi_watcher")

    try:
        config = load_config()
        webhook_url = load_webhook_url()
    except Exception as e:
        log.error("설정 로드 실패: %s", e)
        return 2

    try:
        notices = fetch_notices(
            config["koi_notice_url"],
            timeout=config.get("request_timeout_seconds", 15),
        )
    except Exception as e:
        log.error("KOI 페이지 fetch 실패: %s", e)
        return 1

    if not notices:
        log.warning("공지가 0건 파싱됨 — 페이지 구조가 바뀌었을 수 있음")
        return 1

    log.info("총 %d건 파싱됨", len(notices))

    seen = load_seen()
    first_run = not SEEN_PATH.exists()

    if first_run:
        # 첫 실행은 알림 보내지 않고 baseline 만 기록
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
    # 오래된 것부터 보내도록 reverse (페이지는 최신순이므로)
    for notice in reversed(new_items):
        try:
            send_discord(
                webhook_url,
                config.get("webhook_username", "KOI 공지 알리미"),
                notice,
                config["koi_base_url"],
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
