# activity_log

음성으로 남긴 활동 기록을 타임라인으로 보여주는 개인용 시간 기록 서버.

아이폰 단축어(음성 받아쓰기), 브라우저 입력칸, 터미널 어디서든 한 줄로 기록하면
LLM이 카테고리를 분류하고, 하루가 타임블록으로 그려지고, 아침에 요약이 텔레그램으로 온다.

## 무엇을 하나

- **기록**: `POST /log` 한 방. "설거지", "회의 시작", "설거지 끝" 같은 자연어 한 줄
- **종료 마커**: `~끝`/`~종료`로 끝나는 기록은 앞서 열린 같은 활동을 찾아 종료 시각으로 확정.
  겹쳐 진행되는 병렬 활동(밥 먹으며 설거지)도 각각 닫힌다
- **타임라인**: 하루 24시간 세로 타임블록. 1~7일 보기, 핀치 줌, 겹치는 활동은 나란히 표시,
  블록을 누르면 텍스트·카테고리·시작/종료 시각을 그 자리에서 수정
- **커버리지**: 00:00부터 지금까지 중 기록으로 설명되는 시간 비율을 실시간 표시
- **분류**: 기록 후 잠시 뒤 Claude CLI가 카테고리(업무/식사/육아/킬링타임 등)를 자동 분류
- **통계**: 최근 7일 카테고리별 스택 바 + 합계·일평균·비율
- **브리핑**: 매일 아침 전날 요약을 텔레그램으로 전송
- **백업**: 매일 SQLite 온라인 백업 + 텔레그램으로 오프사이트 전송

## 스택

Python 3.12 · FastAPI · SQLite(표준 라이브러리) · systemd user service/timer.
프런트엔드는 서버가 그리는 단일 HTML(빌드 없음, 의존성 없음).
분류는 Claude Code CLI를 헤드리스(`claude -p`)로 호출한다 — 별도 API 키가 필요 없다.

## 설치

```bash
git clone <this-repo> activity_log && cd activity_log
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp .env.example .env && chmod 600 .env
# .env에 ACTIVITY_TOKEN(생성), TELEGRAM_LOG_BOT(선택), CLAUDE_BIN(선택)을 채운다
python3 -c "import secrets; print(secrets.token_hex(16))"   # 토큰 생성

git config core.hooksPath scripts/git-hooks   # 민감정보 커밋 차단 훅 활성화
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787
```

`http://127.0.0.1:8787/timeline` 접속.

상시 운영은 systemd user service로 띄우고, 분류·백업·브리핑은 타이머로 돌린다
(`scripts/classify.py`, `scripts/backup.py`, `scripts/briefing.py`).

## 기록 보내기

```bash
curl -s -X POST http://<host>:8787/log \
  -H "X-Token: $ACTIVITY_TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"설거지","source":"manual"}'
```

아이폰은 단축어에서 "받아쓰기 → URL 콘텐츠 가져오기"로 같은 요청을 보내면 된다.

## 보안 메모

이 서버는 인증이 토큰 한 개뿐이므로 **공개 인터넷에 노출하지 말 것.**
Tailscale 같은 사설 네트워크 주소에만 바인딩해서 쓰는 것을 전제로 한다.
개인 설정값(토큰·서버 주소·경로)은 전부 `.env`에 두며 저장소에 넣지 않는다.
기록 데이터(`data/`), 로그, 백업도 커밋 대상이 아니다.

## 라이선스

MIT
