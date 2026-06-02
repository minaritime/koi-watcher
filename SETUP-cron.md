# 외부 cron 으로 자동 실행 보장 (GitHub 예약 우회)

GitHub Actions 의 `schedule`(cron) 트리거는 best-effort 라 실행이 수 시간 지연되거나
누락된다(이 저장소 실측: KOI 매시 cron 이 1~6시간 간격으로만 실행, 림버스 `*/10`은 16시간 미실행).

반면 **`workflow_dispatch`(수동 실행)는 호출 즉시 100% 실행**된다. 따라서 신뢰할 수 있는
외부 무료 cron 서비스가 10분마다 GitHub API 로 dispatch 를 호출하게 만들면,
노트북이 꺼져 있어도(클라우드) 정시에 자동 실행이 보장된다.

```
cron-job.org (24시간, 정시) ──10분마다 GitHub API──▶ workflow_dispatch ▶ 워처 즉시 실행 ▶ 디스코드
```

---

## 1단계 — GitHub PAT(토큰) 발급

dispatch 를 호출하려면 Actions 쓰기 권한 토큰이 필요하다.

1. https://github.com/settings/personal-access-tokens → **Fine-grained tokens** → **Generate new token**
2. 설정:
   - **Token name**: `cron-dispatch`
   - **Expiration**: 1년(최대) — 만료되면 갱신 필요
   - **Repository access**: **Only select repositories** → `minaritime/koi-watcher`
   - **Permissions → Repository permissions → Actions**: **Read and write**
   - (그 외 권한 불필요)
3. **Generate token** → 토큰 문자열 복사(한 번만 보임). 예: `github_pat_XXXX...`

> 기존 `KEEPALIVE_PAT` 에 Actions: Read and write 권한을 추가해 재사용해도 된다.

---

## 2단계 — cron-job.org 가입 + 작업 2개 등록

1. https://cron-job.org 무료 가입(이메일)
2. **CREATE CRONJOB** 클릭. 아래 2개를 각각 등록.

### 작업 ① 림버스 (10분마다)

| 항목 | 값 |
|------|----|
| Title | `limbus dispatch` |
| URL | `https://api.github.com/repos/minaritime/koi-watcher/actions/workflows/limbus.yml/dispatches` |
| Schedule | Every 10 minutes (`*/10`) |
| Request method | **POST** |

**Advanced → Headers** 에 추가:
```
Accept: application/vnd.github+json
Authorization: Bearer github_pat_여기에토큰
X-GitHub-Api-Version: 2022-11-28
User-Agent: cron-job
```

**Advanced → Request body**:
```json
{"ref":"main"}
```

### 작업 ② KOI (정올, 15분마다 권장)

①과 동일하되:
| 항목 | 값 |
|------|----|
| Title | `koi dispatch` |
| URL | `https://api.github.com/repos/minaritime/koi-watcher/actions/workflows/notify.yml/dispatches` |
| Schedule | Every 15 minutes |

Headers / Body 는 ①과 동일(같은 토큰 사용).

---

## 3단계 — 확인

- cron-job.org 작업 저장 후, 잠시 뒤 GitHub → Actions 탭에서
  **`workflow_dispatch` 이벤트 실행**이 10/15분마다 뜨는지 확인.
- 정상이면 응답 코드 **204**(성공, 본문 없음). 401/403 이면 토큰 권한 확인.

## 동작 원리 / 주의

- dispatch 가 워처를 즉시 실행 → 새 공지 있으면 디스코드 전송, 없으면 "새 공지 없음".
- GitHub `schedule` cron 은 그대로 둬도 무방(백업). 같은 공지는 seen.json 으로 중복 방지됨.
- 토큰 만료(1년) 시 cron-job.org Header 의 토큰만 새로 교체하면 된다.
