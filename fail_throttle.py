"""연속 실패 시 GitHub 실패 메일을 1시간에 1통으로 줄인다.

GitHub 는 워크플로우가 실패로 끝날 때마다 메일을 보낸다. 알리미는 15분마다
돌기 때문에 소스 사이트가 고장 나면 메일이 쏟아진다. 시도는 그대로 계속하되,
실패 보고(= 종료 코드 != 0)는 마지막 보고 후 REPORT_INTERVAL_SECONDS 가
지났을 때만 하고 나머지 실패는 성공(0)으로 끝내 메일을 막는다.

상태 파일은 보고할 때와 회복할 때만 바뀐다 → 실패 중에도 커밋은 시간당 1번.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

REPORT_INTERVAL_SECONDS = 3600
SCHEDULE_JITTER_SECONDS = 300   # 외부 cron 호출 간격이 조금씩 흔들려도 1시간 주기를 놓치지 않게


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def run_throttled(main: Callable[[], int], state_path: Path, log: logging.Logger) -> int:
    """main() 을 실행하고, 실패 보고를 시간당 1회로 제한한 종료 코드를 돌려준다."""
    try:
        code = main()
    except Exception:
        log.exception("처리되지 않은 예외")
        code = 1

    state = _load(state_path)
    now = int(time.time())

    if code == 0:
        if state:
            log.info("실패 상태에서 회복됨 (실패 시작: %s)", state.get("first_fail_at"))
            _save(state_path, {})
        return 0

    last = state.get("last_reported_ts", 0)
    if now - last >= REPORT_INTERVAL_SECONDS - SCHEDULE_JITTER_SECONDS:
        _save(state_path, {
            "first_fail_at": state.get("first_fail_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "last_reported_ts": now,
        })
        return code

    log.warning("연속 실패 중 — 실패 메일은 1시간에 1번만 보내므로 이번 실행은 성공으로 종료")
    return 0
