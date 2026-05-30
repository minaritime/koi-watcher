# KOI 공지 알리미

[koi.or.kr/notice](https://koi.or.kr/notice) 를 1시간마다 확인해 새 공지가
올라오면 디스코드 채널로 `@everyone` 알림을 보낸다.

GitHub Actions 의 cron 으로 클라우드에서 자동 실행되므로 PC 가 꺼져 있어도
동작한다.

## 구성

| 파일 | 역할 | git |
|------|------|-----|
| `koi_watcher.py` | 메인 스크립트 | ✅ 커밋 |
| `config.json` | 비밀 아닌 설정 (URL, 이름, 타임아웃) | ✅ 커밋 |
| `secrets.json` | 디스코드 웹훅 URL (로컬 전용) | ❌ gitignore |
| `seen.json` | 이미 본 공지 ID 목록 (상태) | ✅ 커밋 (워크플로가 갱신) |
| `requirements.txt` | Python 의존성 | ✅ 커밋 |
| `.github/workflows/notify.yml` | GitHub Actions 워크플로 | ✅ 커밋 |

## GitHub 저장소 셋업

1. **GitHub 에서 private 저장소 생성**
   - 이름 예: `koi-watcher`
   - **반드시 private** (코드 자체에 비밀은 없지만 안전 확보용)

2. **로컬에서 git init + push**
   ```powershell
   cd C:\claude\koi_watcher
   git init
   git add .
   git commit -m "init: KOI 공지 알리미"
   git branch -M main
   git remote add origin https://github.com/<USERNAME>/koi-watcher.git
   git push -u origin main
   ```

3. **GitHub Secret 등록** (웹훅 URL)
   - 저장소 페이지 → **Settings → Secrets and variables → Actions**
   - **New repository secret** 클릭
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: (디스코드 웹훅 URL 그대로 붙여넣기)

4. **워크플로 권한 확인**
   - **Settings → Actions → General → Workflow permissions**
   - **Read and write permissions** 선택 → Save
   - (seen.json 을 워크플로가 다시 커밋해야 하므로)

5. **수동 첫 실행으로 검증**
   - 저장소 → **Actions 탭 → KOI Notice Watcher → Run workflow**
   - 로그 확인 (성공 시 "새 공지 없음" 또는 "전송 완료" 표시)

## 로컬에서 테스트 실행

```powershell
cd C:\claude\koi_watcher
python koi_watcher.py
```

웹훅은 `secrets.json` 에서 읽는다 (gitignore 됨).

## 동작 원리

- **첫 실행**: 현재 페이지의 모든 공지를 baseline 으로 기록만 하고 알림은 안 보냄
- **이후 실행**: 새 공지만 발견 → 디스코드 전송 → `seen.json` 갱신
- GitHub Actions 실행 후 `seen.json` 이 변경되면 봇이 자동으로 커밋해 다음 실행에 반영

## 주기 변경

`.github/workflows/notify.yml` 의 cron 식 수정:

| 주기 | cron |
|------|------|
| 5분 | `*/5 * * * *` |
| 15분 | `*/15 * * * *` |
| 30분 | `*/30 * * * *` |
| 1시간 (기본) | `0 * * * *` |

> GitHub Actions cron 은 무료 한도 안에서 5~15분 정도 지연이 흔하다.

## 새 공지 누락 대응

KOI 페이지 HTML 구조가 바뀌면 파싱이 0건이 될 수 있다. 그럴 때
`koi_watcher.py` 의 `parse_notices()` 의 셀렉터를 갱신한다.

```python
link = row.select_one("td.title-cell a.u-url")
date_cell = row.select_one("td.date-cell")
```

## 웹훅 URL 노출됐을 때

디스코드에서 채널 편집 → 통합 → 웹훅 → URL 재설정 (Reset Webhook URL) 후
- 로컬 `secrets.json` 갱신
- GitHub Secret `DISCORD_WEBHOOK_URL` 도 새 값으로 업데이트
