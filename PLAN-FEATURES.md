# 기능 확장 계획 (1~6)

대상: `app/main.py` 중심. 원칙 유지: raw_text 절대 불변, 배치 멱등성, Tailscale 내부망 전제(쓰기 API는 X-Requested-With 헤더 가드).

## 1. 새벽 시간대 + 자정 넘김 (핵심)

현재 문제: 축이 06~24시 고정이라 새벽 기록이 안 보임. 수면(23:00→익일 07:00) 같은 자정 넘김 활동이 다음 날로 이어지지 않음. HH:MM 종료 입력이 자정을 못 넘음.

### 1a. natural_end의 자정 넘김 허용
- 지금: 하루 안의 다음 이벤트까지만. 그날 마지막 이벤트는 오늘이면 now, 과거면 24:00에서 끝.
- 변경: **전역 다음 이벤트**(날짜 무관)까지 이어짐. 상한: `min(전역 다음 시작, start+24h)`. 전역 다음이 없으면 오늘→now, 과거→해당일 24:00 (지금과 동일).
- 명시적 end_ts도 동일 상한(다음 이벤트 시작, start+24h).

### 1b. 날짜별 렌더링 = 클리핑
- 날짜 D의 블록 = (D의 이벤트들) + (D 이전 마지막 이벤트 중 end가 D 00:00을 넘는 것의 **이월(carry-over) 블록**, 00:00부터 클리핑).
- 이월 블록은 라벨 앞에 "↪" 표시, 편집(종료 시각)은 원본 이벤트 id로 동작.
- 합계(totals)는 표시일에 클리핑된 분량만 집계 → 날짜 간 이중 집계 없음.

### 1c. 동적 축
- 표시 범위 내 블록(이월 포함)이 06시 이전에 존재하면 축 시작을 00시로 확장(그 외 06시 유지). 축 끝은 24시 고정.
- 템플릿의 DAY_START_HOUR/TOTAL_MIN 상수를 렌더 시 동적 값으로 주입 (JS의 nowline·현재 버튼 계산 포함).

### 1d. 종료 입력 자정 넘김
- `set_event_end`: HH:MM 입력이 시작보다 이르면 **다음 날로 해석** (`end += 1일`). 24h 초과는 400.

## 2. git 초기화 + DB 백업
- `git init` + 초기 커밋 (.gitignore에 `backups/` 추가; data/, logs/, .venv/, .env 기존 유지).
- `scripts/backup.py`: sqlite3 backup API로 `backups/activity-YYYYMMDD.db` 생성, 14일 초과분 삭제.
- systemd user 타이머 `activity-backup.timer` 매일 02:00 (+ Persistent=true).

## 3. 카테고리 수동 수정
- `POST /events/{id}/category` body `{"category": str|null}` — CATEGORIES 목록 검증, null이면 NULL로 리셋(다음 새벽 배치가 재분류). X-Requested-With 가드.
- 블록에 `data-category` 추가, 편집 패널에 카테고리 select(11종 + "자동 재분류").

## 4. 주간 통계 /stats
- `GET /stats?end=YYYY-MM-DD` (기본 오늘): end 포함 직전 7일.
- 서버 렌더 단일 HTML: 일자별 카테고리 스택 바(CSS, 기존 카테고리 색), 기간 합계 칩, 카테고리별 합계·비율 표, 킬링타임 비율 강조. ◀▶ 7일 이동, 타임라인 ↔ 스탯 상호 링크.
- 데이터는 1b의 클리핑 로직 재사용(수면 이월 반영).

## 5. 기록 수정/숨김 (raw_text 불변 유지)
- 스키마: `hidden INTEGER NOT NULL DEFAULT 0`, `display_text TEXT` 컬럼 추가 (마이그레이션: PRAGMA로 존재 확인 후 ALTER).
- 표시 = `COALESCE(display_text, raw_text)`. 분류 배치는 raw_text 기준 유지.
- `POST /events/{id}/edit` `{"text": str|null}` (null=원문 복원), `POST /events/{id}/hide` `{"hidden": bool}`.
- fetch_events는 hidden=0만. `/events?include_hidden=1`로 숨김 포함 조회 가능.
- 편집 패널 확장(2행): 1행 텍스트 input / 2행 [카테고리][종료 시각][저장][숨김][닫기]. 저장은 변경된 필드만 각 엔드포인트로 전송 후 새로고침.

## 6. 홈 화면 설치 (PWA-lite)
- `app/static/manifest.webmanifest` + 아이콘 PNG(192/512/180, pillow로 생성: 액센트 배경 + 시계 도형).
- 타임라인 head에 manifest/apple-touch-icon/theme-color/standalone 메타 추가.
- 한계 명시: http(비보안 컨텍스트)라 안드로이드 크롬의 "완전한 PWA 설치"는 불가 → 홈 화면 바로가기+아이콘+독립 실행(iOS)까지 지원. (원하면 추후 `tailscale serve`로 https 제공 가능 — 범위 외)

## 구현 순서
1(모델·축) → 5(스키마 확장, 편집 패널 개편과 3의 UI 통합) → 3 → 2 → 4 → 6

## 플랜 리뷰 반영 결정 (에이전트 3명, 전원 approve_with_changes)

- **1a 수정**: natural_end = 전역 다음이 start+24h 이내면 그 시작까지 / 전역 다음 없으면 min(now, start+24h) (날짜 무관 — 자정 넘겨도 '진행 중' 유지) / 전역 다음이 24h 초과 뒤면 원본일 24:00 (유령 이월 방지). ongoing = 전역 마지막 & now < start+24h.
- **1b 수정**: 모든 집계·방출 지점에서 양쪽 클리핑 [max(start, D 00:00), min(end, D+1 00:00)]. 이월 판정은 strict(end > D 00:00), empty/마커 블록은 이월 제외.
- **1c 폐기 → 단순화**: 축 00~24 고정. 로드 시 초기 스크롤(오늘=현재-2h, 과거=06시)로 대체.
- **1d 강화**: 시작을 분 단위 절단 후 비교, 같으면 400. 익일 해석 duration > 18h는 400. 클라이언트는 익일 해석 시 confirm.
- **5 확정**: 숨김은 시간 경계 유지, 렌더/집계만 제외(빈 시간으로 표시). ?hidden=1 모드에서 반투명 표시 + 해제 버튼. 숨김 버튼은 confirm 필수. 마커 판정은 raw_text 고정, 표시 치환은 build_blocks 1곳.
- **분류 입력 변경**: classify.py는 COALESCE(display_text, raw_text) 기준으로 분류(STT 교정 반영). 텍스트 수정 시 category 자동 리셋(재분류 유도). 재분류 시 effective_ts 보존.
- **3**: CATEGORIES를 app/constants.py로 공용화. select 라벨 "자동 재분류(다음 배치)".
- **편집 패널**: dirty 추적은 입력 이벤트 기반(값 비교 금지), 종료 input 자동 now 채움 제거(placeholder), 비우고 저장=해제. 2행 flex-wrap.
- **4**: 바는 24h 고정 높이(미기록=여백), 세그먼트 순서 CATEGORY_COLORS 순, 미분류 회색 포함.
- **6**: 아이콘은 1회 생성해 커밋, pillow는 생성 후 제거. 안드로이드는 "브라우저 바로가기 수준" 한계 명시.
- **인덱스**: idx_events_eff ON events(COALESCE(effective_ts, ts)) 추가.
- Defer 기록: 없음. (완료됨: 시작 시각 수정 UI, 병렬 활동 마커 매칭, 즉시 분류, 익일 종료 UI, 백업 오프사이트 — 2026-08-17)
- **즉시 분류 (2026-08-17)**: POST /log·텍스트 수정·카테고리 리셋 시 90초 디바운스(threading.Timer) 후
  classify.py를 subprocess로 실행. 실행은 lock으로 직렬화, 실패는 로그만(야간 01:00 배치가 안전망).
  서버 시작 시 미분류 잔존하면 1회 예약. CLASSIFY_DEBOUNCE_SEC 환경변수로 조정 가능.
- **익일 종료 UI (2026-08-17)**: 휠에서 시작 이전 시각 선택 시 confirm("다음 날 HH:MM") 후 허용,
  표시는 "익일 HH:MM". 18h 초과는 클라이언트에서도 차단. endNextDay 플래그로 시작 편집 검증 분기.
- **백업 오프사이트 (2026-08-17)**: backup.py가 백업 후 텔레그램 sendDocument(무음)로 DB 파일 전송.
  토큰/챗ID 없거나 실패 시 로그만 남기고 백업은 성공 처리.
- **병렬 활동 (2026-08-17)**: "X 끝" 마커는 직전 1건이 아니라 최근 24h 내 열린 활동을 최신순 탐색해 매칭.
  매칭된 마커는 note='consumed'로 저장되어 타임블록 경계에서 제외(진행 중인 다른 활동을 자르지 않음).
  명시적 end_ts는 다음 이벤트 시작으로 절단하지 않음(start+24h만 상한) — 겹침은 컬럼 분할로 표시, 통계는 각각 집계.
- **시작 시각 수정 (2026-08-17)**: POST /events/{id}/start (HH:MM=당일 해석, null=effective_ts 해제).
  종료보다 늦으면 400, 18h 초과 400. UI는 종료와 동일한 휠 피커 공용(editTarget 'start'|'end').

## 리스크
- 1의 클리핑/이월은 build_blocks 재작성 수준 — 기존 동작(빈 시간, 진행 중, 겹침 컬럼)과의 회귀 위험이 가장 큼. 검증 필수.
- 편집 패널 개편은 모바일 폭에서 레이아웃 확인 필요.
- pillow 설치 실패 시 아이콘은 SVG 대체.
