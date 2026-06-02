"""림버스 컴퍼니 공식 공지 → 디스코드 알림 (아카라이브 '공식정보' 탭 기반).

아카라이브 '로보토미 코퍼레이션' 채널(/b/lobotomycoperation)의 '공식정보' 카테고리에는
신뢰 유저들이 프로젝트문 공식 공지(업데이트/점검/이벤트/티저 등)를 그대로 옮겨 올린다.
이 탭을 폴링해서, 이전에 보지 못한 새 글이 있으면 디스코드 웹훅으로 전송한다.

기존에는 Steam 스토어 이벤트 API 를 썼으나, 프로젝트문이 Steam 에 올리지 않거나
아카라이브에 먼저 올리는 공지가 많아 누락이 생겨서, 상위집합인 아카 '공식정보' 탭으로 교체함.

첫 실행 시에는 현재 보이는 모든 글을 "이미 봤음" 상태로만 기록하고
알림은 보내지 않는다 (스팸 방지). seen 파일은 limbus_arca_seen.json (Steam 시절과 분리).
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
from urllib.parse import quote

import requests

KST = timezone(timedelta(hours=9))

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "limbus_config.json"
SECRETS_PATH = SCRIPT_DIR / "secrets.json"
SEEN_PATH = SCRIPT_DIR / "limbus_arca_seen.json"   # Steam 시절(limbus_seen.json)과 분리
LOG_PATH = SCRIPT_DIR / "limbus_watcher.log"

ARCA_BASE = "https://arca.live"
# 아카라이브는 봇 차단은 없지만 브라우저 UA 가 아니면 응답이 달라질 수 있어 명시.
_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

# 본문 이미지가 아닌 것들 (이모지/아바타 등) 제외용
_SKIP_IMG = re.compile(r"(twemoji|/emoticon/|gravatar|/static/|\.svg(\?|$))", re.I)


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


def _abs_img(url: str) -> str:
    url = html.unescape(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    return url


# ──────────────────────────────────────────────────────────────────────────
# 목록 파싱 (공식정보 탭) — 글 id/제목/게시일만 가볍게 추출
# ──────────────────────────────────────────────────────────────────────────

def list_url(config: dict) -> str:
    channel = config.get("channel", "lobotomycoperation")
    category = config.get("category", "공식정보")
    return f"{ARCA_BASE}/b/{channel}?category={quote(category)}"


def post_url(config: dict, post_id: str) -> str:
    return f"{ARCA_BASE}/b/{config.get('channel', 'lobotomycoperation')}/{post_id}"


def fetch_listing(config: dict) -> list[dict]:
    resp = requests.get(
        list_url(config),
        headers=_UA,
        timeout=config.get("request_timeout_seconds", 15),
    )
    resp.raise_for_status()
    return parse_listing(resp.text, config)


def parse_listing(page_html: str, config: dict) -> list[dict]:
    """공식정보 탭 HTML → [{id, title, date}, ...] (광고 행 제외)."""
    channel = config.get("channel", "lobotomycoperation")
    id_re = re.compile(r"/b/" + re.escape(channel) + r"/(\d+)")

    notices: list[dict] = []
    seen_ids: set[str] = set()
    for row in re.split(r'<a class="vrow column', page_html)[1:]:
        head = row[:80]
        if "notice-service" in head:        # 광고 행 스킵
            continue
        m_id = id_re.search(row)
        if not m_id:
            continue
        pid = m_id.group(1)
        if pid in seen_ids:                 # 같은 글 중복 매치 방지
            continue

        m_title = re.search(r'col-title">(.*?)</a>', row, re.S)
        raw = m_title.group(1) if m_title else ""
        raw = re.sub(r'<span class="badge.*?</span>', "", raw, flags=re.S)  # 카테고리 배지 제거
        title = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if not title:
            continue

        m_dt = re.search(r'datetime="([^"]+)"', row)
        if m_dt:
            try:
                dt = datetime.fromisoformat(m_dt.group(1).replace("Z", "+00:00"))
                date_str = dt.astimezone(KST).strftime("%Y-%m-%d")
            except ValueError:
                date_str = ""
        else:
            date_str = ""

        seen_ids.add(pid)
        notices.append({"id": pid, "title": title, "date": date_str})

    return notices


# ──────────────────────────────────────────────────────────────────────────
# 글 본문 보강 (새 글만 호출) — 본문 텍스트/이미지/영상 추출
# ──────────────────────────────────────────────────────────────────────────

def extract_body_html(page_html: str) -> str:
    """글 페이지에서 본문 영역(div.fr-view.article-content)의 안쪽 HTML 만 잘라낸다.

    추천/공유/댓글 UI 가 딸려오지 않도록, 본문 div 가 정확히 닫히는 지점까지
    <div> 깊이를 추적해서 매칭한다.
    """
    k = page_html.find('fr-view article-content')
    if k < 0:
        return ""
    start_div = page_html.rfind("<div", 0, k)
    if start_div < 0:
        return ""
    inner_start = page_html.find(">", k) + 1
    depth = 0
    for m in re.finditer(r"<div\b|</div>", page_html[start_div:]):
        if m.group(0) == "</div>":
            depth -= 1
            if depth == 0:
                return page_html[inner_start:start_div + m.start()]
        else:
            depth += 1
    return page_html[inner_start:]


def extract_images(body_html: str) -> list[str]:
    images: list[str] = []
    # 본문 이미지는 src 또는 data-src 에 들어있다. twemoji/아바타 등은 제외.
    for m in re.finditer(r'<img[^>]+>', body_html):
        tag = m.group(0)
        src = re.search(r'(?:data-src|src)="([^"]+)"', tag)
        if not src:
            continue
        url = _abs_img(src.group(1))
        if _SKIP_IMG.search(url):
            continue
        if not url.startswith("http"):
            continue
        if url not in images:
            images.append(url)
    return images


def extract_video(body_html: str) -> str | None:
    m = (re.search(r'youtube\.com/embed/([\w-]+)', body_html)
         or re.search(r'youtu\.be/([\w-]+)', body_html)
         or re.search(r'data-embed="([\w-]+)"', body_html))
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def clean_text(body_html: str) -> str:
    """본문 HTML 을 사람이 읽을 평문으로. (▶ 같은 twemoji 는 alt 텍스트로 복원)"""
    t = body_html
    t = re.sub(r'<img[^>]+class="twemoji"[^>]*alt="([^"]*)"[^>]*>', r"\1", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</p\s*>", "\n\n", t, flags=re.I)
    t = re.sub(r"<img[^>]*>", "", t, flags=re.I)        # 남은 이미지 태그 제거
    t = re.sub(r"<[^>]+>", "", t)                        # 나머지 태그 제거
    t = html.unescape(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def enrich(notice: dict, config: dict) -> dict:
    """새 글의 본문 페이지를 받아 url/images/video/text 를 채운다."""
    url = post_url(config, notice["id"])
    resp = requests.get(url, headers=_UA, timeout=config.get("request_timeout_seconds", 15))
    resp.raise_for_status()
    page = resp.text

    m_title = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    if m_title:
        title = html.unescape(m_title.group(1))
        title = re.sub(r"\s*-\s*로보토미 코퍼레이션 채널\s*$", "", title).strip()
        if title:
            notice["title"] = title

    body = extract_body_html(page)
    return {
        **notice,
        "url": url,
        "images": extract_images(body),
        "video": extract_video(body),
        "text": clean_text(body),
    }


# ──────────────────────────────────────────────────────────────────────────
# 디스코드 전송 (Steam 시절과 동일 — 이미지 첨부 / 영상 / 텍스트 분기)
# ──────────────────────────────────────────────────────────────────────────

CONTENT_HEADER = "@everyone 📢 림버스 컴퍼니 새 공지가 올라왔습니다"
EMBED_COLOR = 0xC0392B
MAX_IMAGES = 10                      # 한 메시지에 보여줄 이미지 최대 개수 (디스코드 첨부 한도 = 10)
MAX_TEXT_LEN = 600                   # 텍스트 공지 본문 발췌 길이
PER_FILE_CAP = 24 * 1024 * 1024      # 첨부 1개 최대 용량
TOTAL_UPLOAD_CAP = 24 * 1024 * 1024  # 한 메시지 첨부 총합 최대 용량


def meta_embed(notice: dict) -> dict:
    """제목·게시일·링크를 담은 임베드 (이미지 없는 카드)."""
    embed = {
        "title": notice["title"][:256] or "(제목 없음)",
        "url": notice["url"],
        "color": EMBED_COLOR,
        "footer": {"text": "Limbus Company · 아카라이브 공식정보"},
    }
    if notice.get("date"):
        embed["fields"] = [{"name": "게시일", "value": notice["date"], "inline": True}]
    return embed


def download_images(urls: list[str]) -> list[tuple]:
    """이미지를 내려받아 (파일명, bytes, mime) 목록으로 반환.

    이미지를 "전부" 보여주는 것이 목표라, 한 장이라도 못 받거나 용량 한도를 넘으면
    일부만 첨부해 누락시키지 않고 빈 목록을 반환한다. 그러면 send_discord 가
    URL 임베드 방식으로 폴백해 (용량 제한 없이) 모든 이미지를 표시한다.
    """
    files: list[tuple] = []
    total = 0
    for i, u in enumerate(urls[:MAX_IMAGES]):
        try:
            r = requests.get(u, headers=_UA, timeout=30)
            r.raise_for_status()
            data = r.content
        except Exception:
            return []  # 일부 실패 → 첨부 포기, URL 임베드로 전부 표시
        if len(data) > PER_FILE_CAP or total + len(data) > TOTAL_UPLOAD_CAP:
            return []  # 용량 초과 → 첨부 포기, URL 임베드로 전부 표시
        total += len(data)
        ext = u.split("?")[0].rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "png"
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        files.append((f"limbus_{i}.{ext}", data, mime))
    return files


def build_payload(username: str, notice: dict) -> dict:
    """JSON 전용 페이로드 (영상/텍스트, 그리고 이미지 첨부 실패 시 임베드 URL 폴백)."""
    images = notice.get("images") or []
    video = notice.get("video")
    base = {"username": username, "allowed_mentions": {"parse": ["everyone"]}}

    if images:
        embeds = []
        for i, img in enumerate(images[:MAX_IMAGES]):
            embed = {"url": notice["url"], "image": {"url": img}, "color": EMBED_COLOR}
            if i == 0:
                embed.update(meta_embed(notice))
            embeds.append(embed)
        return {**base, "content": CONTENT_HEADER, "embeds": embeds}

    if video:
        return {
            **base,
            "content": f"{CONTENT_HEADER}\n**{notice['title']}**  "
                       f"(게시일 {notice['date']})\n{notice['video']}",
        }

    text = notice.get("text") or ""
    desc = text[:MAX_TEXT_LEN] + ("…" if len(text) > MAX_TEXT_LEN else "")
    embed = meta_embed(notice)
    embed["description"] = desc or "(본문 없음)"
    return {**base, "content": CONTENT_HEADER, "embeds": [embed]}


def send_discord(webhook_url: str, username: str, notice: dict) -> None:
    log = logging.getLogger("limbus_watcher")

    if notice.get("images"):
        # 원본 이미지를 파일로 첨부해야 확대 시 글자가 선명하다.
        # (임베드 URL 방식은 디스코드 프록시가 압축·축소해서 흐릿함)
        files = download_images(notice["images"])
        if files:
            payload = {
                "username": username,
                "allowed_mentions": {"parse": ["everyone"]},
                "content": CONTENT_HEADER,
                "embeds": [meta_embed(notice)],
            }
            try:
                resp = requests.post(
                    webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files={f"file{i}": f for i, f in enumerate(files)},
                    timeout=90,
                )
                resp.raise_for_status()
                return
            except Exception as e:
                log.warning("이미지 첨부 실패 → 임베드 URL 로 폴백: %s", e)

    # 영상·텍스트, 또는 이미지 첨부 실패 시 폴백
    resp = requests.post(webhook_url, json=build_payload(username, notice), timeout=30)
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
        notices = fetch_listing(config)
    except Exception as e:
        log.error("아카라이브 공식정보 탭 fetch 실패: %s", e)
        return 1

    if not notices:
        log.warning("글이 0건 파싱됨 — 채널/카테고리 또는 페이지 구조를 확인하세요")
        return 1

    log.info("공식정보 탭 %d건 파싱됨", len(notices))

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
    # 목록은 최신순이므로 오래된 것부터 보내도록 reverse
    for notice in reversed(new_items):
        try:
            full = enrich(notice, config)
            send_discord(
                webhook_url,
                config.get("webhook_username", "림버스 공지 알리미"),
                full,
            )
            seen.add(notice["id"])
            save_seen(seen)
            log.info("전송 완료: %s", full["title"])
        except Exception as e:
            log.error("전송 실패 (%s): %s", notice.get("title", notice["id"]), e)
            # 실패 시 seen 에 추가하지 않음 → 다음 실행에서 재시도

    return 0


if __name__ == "__main__":
    sys.exit(main())
