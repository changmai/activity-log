import hashlib
import html as html_lib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.constants import CATEGORIES, CATEGORY_COLORS, UNCATEGORIZED, UNCATEGORIZED_COLOR

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "activity.db"
LOG_PATH = ROOT / "logs" / "server.log"
KST = ZoneInfo("Asia/Seoul")
END_MARKERS = {"끝", "종료", "end"}

DAY_START_HOUR = 0
DAY_END_HOUR = 24
TOTAL_MIN = (DAY_END_HOUR - DAY_START_HOUR) * 60
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

PALETTE = list(CATEGORY_COLORS.values())


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()
ACTIVITY_TOKEN = os.environ.get("ACTIVITY_TOKEN", "")

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("activity_log")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)

app = FastAPI(title="activity_log")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              received_at TEXT NOT NULL,
              raw_text TEXT NOT NULL,
              source TEXT NOT NULL,
              category TEXT,
              tags TEXT,
              effective_ts TEXT,
              note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            """
        )
    with get_db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
        if "end_ts" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN end_ts TEXT")
        if "hidden" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        if "display_text" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN display_text TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_eff ON events(COALESCE(effective_ts, ts))"
        )


init_db()


# ---- 즉시 분류: 기록 후 디바운스 시간 내 추가 기록이 없으면 classify.py 실행 ----
# (연속 발화를 한 번의 claude CLI 호출로 묶음. 새벽 01:00 배치는 재시도 안전망으로 유지)
CLASSIFY_DEBOUNCE_SEC = float(os.environ.get("CLASSIFY_DEBOUNCE_SEC", "90"))
_classify_timer: Optional[threading.Timer] = None
_classify_timer_lock = threading.Lock()
_classify_run_lock = threading.Lock()  # 실행 직렬화 — claude CLI 동시 호출 방지


def _run_classify() -> None:
    with _classify_run_lock:
        try:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "classify.py")],
                capture_output=True, text=True, timeout=700,
            )
            logger.info("instant classify done rc=%s", proc.returncode)
        except Exception as e:  # 실패해도 서비스에 영향 없음 — 야간 배치가 재시도
            logger.warning("instant classify failed: %s", e)


def schedule_classify() -> None:
    global _classify_timer
    with _classify_timer_lock:
        if _classify_timer is not None:
            _classify_timer.cancel()
        _classify_timer = threading.Timer(CLASSIFY_DEBOUNCE_SEC, _run_classify)
        _classify_timer.daemon = True
        _classify_timer.start()


# 서버 시작 시 미분류가 남아 있으면 한 번 예약 (재시작으로 놓친 분류 보충)
with get_db() as _c:
    if _c.execute("SELECT 1 FROM events WHERE category IS NULL LIMIT 1").fetchone():
        schedule_classify()


def now_kst() -> datetime:
    return datetime.now(KST)


def is_end_marker(text: str) -> bool:
    """종료 마커 판정: '끝'/'종료'/'end' 단독 또는 그 단어로 끝나는 문장.
    예: "빨래 끝" → 해당 시각부터 빈 시간. 판정은 항상 raw_text 기준."""
    t = text.strip().lower()
    return t in END_MARKERS or t.endswith(("끝", "종료", " end"))


_MARKER_SUFFIX_RE = re.compile(r"^(.*?)(?:끝|종료|end)\s*$", re.IGNORECASE)


def _normalize_text(t: str) -> str:
    return re.sub(r"\s+", "", t).lower()


def match_marker_to_activity(text: str, marker_ts: datetime) -> Optional[int]:
    """'X 끝' 마커의 X와 (띄어쓰기 무시) 동일/유사한 열린 활동을 찾아
    그 활동의 end_ts를 마커 시각으로 확정하고 이벤트 id를 반환.

    매칭 조건: 정규화 후 동일, 포함 관계, 또는 유사도 ≥ 0.75.
    병렬 활동 지원: 직전 1건이 아니라 최근 24h 내 열린 활동들을 최신순으로 거슬러
    올라가며 텍스트가 맞는 첫 활동을 닫는다 (예: 설거지 → 밥 먹기 → "설거지 끝").
    """
    m = _MARKER_SUFFIX_RE.match(text.strip())
    base = m.group(1).strip() if m else ""
    if not base:
        return None  # 단독 "끝" — 매칭 대상 없음
    with get_db() as conn:
        candidates = conn.execute(
            "SELECT * FROM events WHERE COALESCE(effective_ts, ts) < ? "
            "AND COALESCE(effective_ts, ts) >= ? "
            "ORDER BY COALESCE(effective_ts, ts) DESC, id DESC LIMIT 30",
            (
                marker_ts.isoformat(timespec="seconds"),
                (marker_ts - timedelta(hours=24)).isoformat(timespec="seconds"),
            ),
        ).fetchall()
        a = _normalize_text(base)
        for prev in candidates:
            if is_end_marker(prev["raw_text"]) or prev["end_ts"] or prev["hidden"]:
                continue
            prev_start = parse_ts(prev["effective_ts"] or prev["ts"])
            if prev_start is None or not (prev_start < marker_ts <= prev_start + timedelta(hours=24)):
                continue
            b = _normalize_text(prev["display_text"] or prev["raw_text"])
            if not (a == b or a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.75):
                continue
            conn.execute(
                "UPDATE events SET end_ts = ? WHERE id = ?",
                (marker_ts.isoformat(timespec="seconds"), prev["id"]),
            )
            logger.info("marker match: '%s' → id=%s end_ts=%s", base, prev["id"],
                        marker_ts.isoformat(timespec="seconds"))
            return prev["id"]
        return None


class LogIn(BaseModel):
    text: str = ""
    ts: Optional[str] = None
    source: str = "voice"


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/log")
def post_log(body: LogIn, x_token: str = Header(default=""),
             x_requested_with: str = Header(default="")):
    # 인증: 단축어/터미널은 X-Token, 타임라인 빠른 입력칸은 X-Requested-With
    # (커스텀 헤더는 cross-origin에서 preflight 없이 보낼 수 없음 — 편집 API들과 동일 수준)
    token_ok = bool(ACTIVITY_TOKEN) and x_token == ACTIVITY_TOKEN
    if not (token_ok or x_requested_with == "timeline"):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    text = body.text.strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text is empty"}, status_code=400)
    # ts는 ISO8601만 신뢰. 파싱 불가한 형식(예: "2026. 8. 17. 오전 11:11")이면 서버 시각 사용
    parsed = parse_ts(body.ts) if body.ts else None
    ts_dt = parsed or now_kst()
    ts = ts_dt.isoformat(timespec="seconds")
    received_at = now_kst().isoformat(timespec="seconds")
    # "X 끝" 마커면 열린 활동 X의 종료 시각으로 확정 기록 시도
    matched_id = match_marker_to_activity(text, ts_dt) if is_end_marker(text) else None
    # 매칭에 성공한 마커는 note='consumed' — end_ts로 이미 반영됐으므로 타임블록
    # 경계로 쓰지 않는다 (병렬 활동: "설거지 끝"이 진행 중인 다른 활동을 자르면 안 됨)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO events (ts, received_at, raw_text, source, note) VALUES (?, ?, ?, ?, ?)",
            (ts, received_at, text, body.source or "voice",
             "consumed" if matched_id is not None else None),
        )
    logger.info("log id=%s source=%s text=%s", cur.lastrowid, body.source, text)
    schedule_classify()  # 디바운스 후 즉시 분류
    return {"ok": True, "id": cur.lastrowid, "ts": ts, "matched_end": matched_id}


def _ui_forbidden(x_requested_with: str) -> Optional[JSONResponse]:
    # CSRF 차단: 커스텀 헤더는 cross-origin에서 preflight 없이 보낼 수 없음 (CORS 미들웨어 없음)
    if x_requested_with != "timeline":
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return None


def _get_event_or_404(conn: sqlite3.Connection, event_id: int):
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return row, None


class EndIn(BaseModel):
    end: Optional[str] = None  # "HH:MM" 또는 ISO8601, null이면 종료 시각 해제


@app.post("/events/{event_id}/end")
def set_event_end(event_id: int, body: EndIn, x_requested_with: str = Header(default="")):
    if err := _ui_forbidden(x_requested_with):
        return err
    with get_db() as conn:
        row, err = _get_event_or_404(conn, event_id)
        if err:
            return err
        if body.end is None:
            conn.execute("UPDATE events SET end_ts = NULL WHERE id = ?", (event_id,))
            logger.info("end cleared id=%s", event_id)
            return {"ok": True, "end_ts": None}
        start = parse_ts(row["effective_ts"] or row["ts"])
        if start is None:
            return JSONResponse({"ok": False, "error": "이벤트 시각을 해석할 수 없음"}, status_code=400)
        value = body.end.strip()
        if len(value) == 5 and value[2] == ":":  # HH:MM → 이벤트 날짜와 결합, 시작보다 이르면 익일로 해석
            try:
                hh, mm = int(value[:2]), int(value[3:5])
            except ValueError:
                return JSONResponse({"ok": False, "error": "잘못된 시각 형식"}, status_code=400)
            if hh > 23 or mm > 59:
                return JSONResponse({"ok": False, "error": "잘못된 시각 형식"}, status_code=400)
            start_min = start.replace(second=0, microsecond=0)
            end_dt = start.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if end_dt == start_min:
                return JSONResponse({"ok": False, "error": "종료가 시작 시각과 같아요"}, status_code=400)
            if end_dt < start_min:
                end_dt += timedelta(days=1)  # 자정 넘김 (예: 23:00 시작 → 07:00 종료)
        else:
            end_dt = parse_ts(value)
        if end_dt is None:
            return JSONResponse({"ok": False, "error": "잘못된 시각 형식"}, status_code=400)
        if end_dt <= start:
            return JSONResponse({"ok": False, "error": "종료 시각이 시작 시각보다 빨라요"}, status_code=400)
        if end_dt - start > timedelta(hours=18):
            return JSONResponse({"ok": False, "error": "18시간을 넘는 활동은 저장할 수 없어요"}, status_code=400)
        end_iso = end_dt.isoformat(timespec="seconds")
        conn.execute("UPDATE events SET end_ts = ? WHERE id = ?", (end_iso, event_id))
    logger.info("end set id=%s end_ts=%s", event_id, end_iso)
    return {"ok": True, "end_ts": end_iso}


class StartIn(BaseModel):
    start: Optional[str] = None  # "HH:MM" (이벤트 당일 기준), null이면 effective_ts 해제(원래 시각 복원)


@app.post("/events/{event_id}/start")
def set_event_start(event_id: int, body: StartIn, x_requested_with: str = Header(default="")):
    if err := _ui_forbidden(x_requested_with):
        return err
    with get_db() as conn:
        row, err = _get_event_or_404(conn, event_id)
        if err:
            return err
        if body.start is None:
            conn.execute("UPDATE events SET effective_ts = NULL WHERE id = ?", (event_id,))
            logger.info("start cleared id=%s", event_id)
            return {"ok": True, "effective_ts": None}
        cur_start = parse_ts(row["effective_ts"] or row["ts"])
        if cur_start is None:
            return JSONResponse({"ok": False, "error": "이벤트 시각을 해석할 수 없음"}, status_code=400)
        value = body.start.strip()
        if len(value) == 5 and value[2] == ":":  # HH:MM → 이벤트 당일 날짜와 결합
            try:
                hh, mm = int(value[:2]), int(value[3:5])
            except ValueError:
                return JSONResponse({"ok": False, "error": "잘못된 시각 형식"}, status_code=400)
            if hh > 23 or mm > 59:
                return JSONResponse({"ok": False, "error": "잘못된 시각 형식"}, status_code=400)
            new_start = cur_start.replace(hour=hh, minute=mm, second=0, microsecond=0)
        else:
            new_start = parse_ts(value)
        if new_start is None:
            return JSONResponse({"ok": False, "error": "잘못된 시각 형식"}, status_code=400)
        end = parse_ts(row["end_ts"]) if row["end_ts"] else None
        if end is not None:
            if new_start >= end:
                return JSONResponse(
                    {"ok": False, "error": "시작 시각이 종료 시각보다 늦어요"}, status_code=400)
            if end - new_start > timedelta(hours=18):
                return JSONResponse(
                    {"ok": False, "error": "18시간을 넘는 활동은 저장할 수 없어요"}, status_code=400)
        start_iso = new_start.isoformat(timespec="seconds")
        conn.execute("UPDATE events SET effective_ts = ? WHERE id = ?", (start_iso, event_id))
    logger.info("start set id=%s effective_ts=%s", event_id, start_iso)
    return {"ok": True, "effective_ts": start_iso}


class CategoryIn(BaseModel):
    category: Optional[str] = None  # null이면 카테고리 리셋 → 다음 배치가 재분류 (effective_ts 유지)


@app.post("/events/{event_id}/category")
def set_event_category(event_id: int, body: CategoryIn, x_requested_with: str = Header(default="")):
    if err := _ui_forbidden(x_requested_with):
        return err
    if body.category is not None and body.category not in CATEGORIES:
        return JSONResponse({"ok": False, "error": "알 수 없는 카테고리"}, status_code=400)
    with get_db() as conn:
        _, err = _get_event_or_404(conn, event_id)
        if err:
            return err
        if body.category is None:
            conn.execute("UPDATE events SET category = NULL, tags = NULL WHERE id = ?", (event_id,))
        else:
            conn.execute("UPDATE events SET category = ? WHERE id = ?", (body.category, event_id))
    logger.info("category id=%s -> %s", event_id, body.category)
    if body.category is None:
        schedule_classify()  # 리셋된 이벤트를 곧바로 재분류
    return {"ok": True, "category": body.category}


class TextIn(BaseModel):
    text: Optional[str] = None  # null/빈 문자열이면 원문(raw_text)으로 복원


@app.post("/events/{event_id}/edit")
def set_event_text(event_id: int, body: TextIn, x_requested_with: str = Header(default="")):
    if err := _ui_forbidden(x_requested_with):
        return err
    with get_db() as conn:
        row, err = _get_event_or_404(conn, event_id)
        if err:
            return err
        new_text = (body.text or "").strip() or None
        if new_text == row["raw_text"]:
            new_text = None  # 원문과 같으면 저장하지 않음
        if new_text == row["display_text"]:
            # 실제 변경 없음 — 카테고리를 건드리지 않고 종료 (no-op 저장 보호)
            return {"ok": True, "display_text": new_text}
        # 텍스트가 실제로 바뀔 때만 카테고리 리셋 → 다음 배치가 교정된 텍스트로 재분류 (effective_ts 유지)
        conn.execute(
            "UPDATE events SET display_text = ?, category = NULL, tags = NULL WHERE id = ?",
            (new_text, event_id),
        )
    logger.info("edit id=%s display_text=%s", event_id, new_text)
    schedule_classify()  # 교정된 텍스트로 곧바로 재분류
    return {"ok": True, "display_text": new_text}


class HideIn(BaseModel):
    hidden: bool


@app.post("/events/{event_id}/hide")
def set_event_hidden(event_id: int, body: HideIn, x_requested_with: str = Header(default="")):
    if err := _ui_forbidden(x_requested_with):
        return err
    with get_db() as conn:
        _, err = _get_event_or_404(conn, event_id)
        if err:
            return err
        conn.execute("UPDATE events SET hidden = ? WHERE id = ?", (1 if body.hidden else 0, event_id))
    logger.info("hide id=%s hidden=%s", event_id, body.hidden)
    return {"ok": True, "hidden": body.hidden}


def fetch_events(date: str) -> list[sqlite3.Row]:
    # 회상 입력은 effective_ts로 재배치되므로 조회/정렬 모두 COALESCE 기준.
    # hidden 이벤트도 포함한다 — 시간 경계는 유지하고 렌더/집계에서만 제외 (숨김이 이전 블록을 늘리지 않게).
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE substr(COALESCE(effective_ts, ts), 1, 10) = ? "
            "ORDER BY COALESCE(effective_ts, ts) ASC, id ASC",
            (date,),
        ).fetchall()


@app.get("/events")
def get_events(
    date: Optional[str] = Query(default=None),
    include_hidden: bool = Query(default=False),
):
    date = date or now_kst().strftime("%Y-%m-%d")
    rows = fetch_events(date)
    out = []
    for r in rows:
        if r["hidden"] and not include_hidden:
            continue
        d = dict(r)
        d["text"] = r["display_text"] or r["raw_text"]
        out.append(d)
    return {"date": date, "events": out}


def parse_ts(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def _compute_end(start: datetime, row: sqlite3.Row, nxt: Optional[datetime], now: datetime):
    """이벤트의 (클리핑 전) 종료 시각 계산. 반환: (end, has_explicit, explicit_dt, natural_end)

    규칙 (플랜 리뷰 반영):
    - 전역 다음 이벤트가 start+24h 이내면 그 시작까지 (자정 넘김 허용)
    - 전역 다음이 없으면 min(now, start+24h) — 날짜와 무관하게 '진행 중' 연장
    - 전역 다음이 24h 초과 뒤면 원본일 24:00 (오늘이면 now까지) — 유령 이월 방지
    - 명시적 end_ts는 위 상한 안에서 우선
    """
    limit = start + timedelta(hours=24)
    own_day_end = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if nxt is not None and nxt <= limit:
        natural = max(nxt, start)
        cap = natural
    elif nxt is None:
        natural = max(start, min(now, limit))
        cap = limit
    else:
        natural = max(start, min(now, own_day_end)) if now < own_day_end else own_day_end
        cap = own_day_end
    explicit = parse_ts(row["end_ts"]) if row["end_ts"] else None
    is_empty = is_end_marker(row["raw_text"])  # 마커 판정은 raw_text 고정
    has_explicit = explicit is not None and not is_empty
    # 명시적 종료는 start+24h만 상한 — 다음 이벤트 시작으로 절단하지 않는다.
    # 병렬 활동 지원: 설거지(12:00~12:30 확정)와 밥 먹기(12:05~)가 겹쳐 표시되어야 함
    end = min(max(explicit, start), limit) if has_explicit else natural
    return end, has_explicit, explicit, natural


def _make_block(row: sqlite3.Row, start: datetime, end: datetime, *, has_explicit: bool,
                explicit: Optional[datetime], ongoing: bool, carry: bool) -> dict:
    is_empty = is_end_marker(row["raw_text"])
    return {
        "id": row["id"],
        "start": start,
        "end": end,
        "orig_start": parse_ts(row["effective_ts"] or row["ts"]),
        "text": row["display_text"] or row["raw_text"],
        "category": row["category"],
        "empty": is_empty,
        "hidden": bool(row["hidden"]),
        "explicit_end": has_explicit,
        "explicit_hhmm": explicit.strftime("%H:%M") if has_explicit and explicit else "",
        "ongoing": ongoing,
        "carry": carry,
    }


def build_blocks(date: str) -> list[dict]:
    """날짜 D의 타임블록. 자정 넘김 이월 포함, 모든 블록은 [D 00:00, D+1 00:00)로 클리핑됨."""
    day0 = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=KST)
    day1 = day0 + timedelta(days=1)
    now = now_kst()
    rows = fetch_events(date)

    events = []
    for row in rows:
        if row["note"] == "consumed":
            continue  # 매칭된 마커: 종료는 end_ts로 반영됨 — 시간 경계로 쓰지 않음
        dt = parse_ts(row["effective_ts"] or row["ts"])
        if dt is not None:
            events.append((dt, row))
    events.sort(key=lambda e: e[0])

    with get_db() as conn:
        last_coal = events[-1][0].isoformat() if events else day0.isoformat()
        nrow = conn.execute(
            "SELECT COALESCE(effective_ts, ts) AS c FROM events "
            "WHERE COALESCE(effective_ts, ts) > ? AND COALESCE(note, '') != 'consumed' "
            "ORDER BY COALESCE(effective_ts, ts) LIMIT 1",
            (last_coal,),
        ).fetchone()
        global_next = parse_ts(nrow["c"]) if nrow else None
        prow = conn.execute(
            "SELECT * FROM events WHERE COALESCE(effective_ts, ts) < ? "
            "AND COALESCE(note, '') != 'consumed' "
            "ORDER BY COALESCE(effective_ts, ts) DESC LIMIT 1",
            (day0.isoformat(),),
        ).fetchone()

    blocks: list[dict] = []

    # 이월(carry-over): 전날 마지막 이벤트가 D 00:00을 strict 하게 넘는 경우 (마커 블록은 제외)
    if prow is not None:
        pstart = parse_ts(prow["effective_ts"] or prow["ts"])
        p_is_empty = is_end_marker(prow["raw_text"])
        if pstart is not None and not p_is_empty:
            p_next = events[0][0] if events else global_next
            pend, p_has_exp, p_exp, p_natural = _compute_end(pstart, prow, p_next, now)
            if pend > day0:
                p_ongoing = (
                    not events and global_next is None and not p_has_exp
                    and now < pstart + timedelta(hours=24)
                )
                blk = _make_block(prow, day0, min(pend, day1), has_explicit=p_has_exp,
                                  explicit=p_exp, ongoing=p_ongoing, carry=True)
                blocks.append(blk)
            # 명시 종료 후 다음 활동까지의 공백이 이 날짜에 걸치면 빈 시간으로 표시
            if p_has_exp:
                gap_start = max(min(pend, day1), day0)
                gap_end = min(p_natural, day1)
                if (gap_end - gap_start).total_seconds() > 30:
                    blocks.append(
                        {
                            "id": None, "start": gap_start, "end": gap_end, "orig_start": None,
                            "text": "", "category": None, "empty": True, "hidden": False,
                            "explicit_end": False, "explicit_hhmm": "", "ongoing": False,
                            "carry": False,
                        }
                    )

    for i, (start, row) in enumerate(events):
        nxt = events[i + 1][0] if i + 1 < len(events) else global_next
        end, has_explicit, explicit, natural = _compute_end(start, row, nxt, now)
        ongoing = (
            nxt is None and not has_explicit
            and not is_end_marker(row["raw_text"])
            and start < now < start + timedelta(hours=24)
        )
        blocks.append(_make_block(row, start, min(end, day1), has_explicit=has_explicit,
                                  explicit=explicit, ongoing=ongoing, carry=False))
        # 명시적 종료와 다음 활동 사이의 공백은 빈 시간으로 표시 (당일 범위 안에서만)
        gap_end = min(natural, day1)
        if has_explicit and (gap_end - min(end, day1)).total_seconds() > 30:
            blocks.append(
                {
                    "id": None, "start": min(end, day1), "end": gap_end, "orig_start": None,
                    "text": "", "category": None, "empty": True, "hidden": False,
                    "explicit_end": False, "explicit_hhmm": "", "ongoing": False, "carry": False,
                }
            )
    return blocks


def _visible_minutes(b: dict) -> int:
    return int((b["end"] - b["start"]).total_seconds() // 60)


def union_minutes(blocks: list[dict], clip_end: Optional[datetime] = None) -> int:
    """기록된(비어있지 않고 숨겨지지 않은) 블록들의 구간 합집합 분. 커버리지 계산용.
    병렬 활동이 겹치는 구간은 한 번만 세어 100%를 넘지 않게 한다."""
    ivs = []
    for b in blocks:
        if b["empty"] or b["hidden"]:
            continue
        s, e = b["start"], b["end"]
        if clip_end is not None:
            e = min(e, clip_end)
        if e > s:
            ivs.append((s, e))
    ivs.sort()
    total = timedelta()
    cs = ce = None
    for s, e in ivs:
        if cs is None:
            cs, ce = s, e
        elif s <= ce:
            ce = max(ce, e)
        else:
            total += ce - cs
            cs, ce = s, e
    if cs is not None:
        total += ce - cs
    return int(total.total_seconds() // 60)


def category_color(category: Optional[str]) -> str:
    if not category:
        return UNCATEGORIZED_COLOR
    if category in CATEGORY_COLORS:
        return CATEGORY_COLORS[category]
    # 목록 밖 카테고리는 md5 기반 고정 색 (hash()는 프로세스마다 달라짐)
    digest = hashlib.md5(category.encode()).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _tint(color: str, alpha: float) -> str:
    r, g, b = _rgb(color)
    return f"rgba({r},{g},{b},{alpha})"


def _tint_solid(color: str, t: float) -> str:
    """흰색과 혼합한 불투명 틴트 — rgba와 같은 색감이지만 뒤 블록이 비치지 않음."""
    r, g, b = _rgb(color)
    return "#%02x%02x%02x" % (
        int(r * t + 255 * (1 - t)),
        int(g * t + 255 * (1 - t)),
        int(b * t + 255 * (1 - t)),
    )


def _shade(color: str, f: float = 0.38) -> str:
    """카테고리 색을 어둡게 — 틴트 배경 위 텍스트용 (애플 캘린더 방식)."""
    r, g, b = _rgb(color)
    return "#%02x%02x%02x" % (int(r * (1 - f)), int(g * (1 - f)), int(b * (1 - f)))


def _text_on(color: str) -> str:
    """배경색 명도에 따라 흰/검 텍스트 선택 (텍스트 가독성)."""
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return "#ffffff" if lum < 0.45 else "#1f2328"


def assign_columns(blocks: list[dict]) -> list[tuple[int, int]]:
    """시간이 겹치는 블록을 나란히 배치하기 위한 (컬럼, 총컬럼수) 할당 — 구글 캘린더 방식."""
    layouts: list = [None] * len(blocks)
    order = sorted(range(len(blocks)), key=lambda i: blocks[i]["start"])
    cluster: list[tuple[int, int]] = []
    col_ends: list = []

    def close_cluster():
        ncols = len(col_ends)
        for idx, col in cluster:
            layouts[idx] = (col, ncols)

    for i in order:
        start = blocks[i]["start"]
        eff_end = max(blocks[i]["end"], start + timedelta(minutes=1))
        if col_ends and all(e <= start for e in col_ends):
            close_cluster()
            cluster, col_ends = [], []
        placed = False
        for c in range(len(col_ends)):
            if col_ends[c] <= start:
                col_ends[c] = eff_end
                cluster.append((i, c))
                placed = True
                break
        if not placed:
            col_ends.append(eff_end)
            cluster.append((i, len(col_ends) - 1))
    close_cluster()
    return layouts


def render_day_column(date: str, blocks: list[dict], show_hidden: bool) -> str:
    axis_start = datetime.strptime(date, "%Y-%m-%d").replace(hour=DAY_START_HOUR, tzinfo=KST)
    layouts = assign_columns(blocks)
    parts = []
    for bi, b in enumerate(blocks):
        start_min = (b["start"] - axis_start).total_seconds() / 60
        end_min = (b["end"] - axis_start).total_seconds() / 60
        start_min = max(0.0, min(start_min, TOTAL_MIN))
        end_min = max(0.0, min(end_min, TOTAL_MIN))
        dur = end_min - start_min
        if end_min <= 0 or dur < 0:
            continue
        hidden_as_empty = b["hidden"] and not show_hidden
        if b["empty"] or hidden_as_empty:
            cls = "block empty"
            style = ""
            label = f'{b["start"].strftime("%H:%M")} 빈 시간'
            data_attrs = ""
        else:
            color = category_color(b["category"])
            cls = "block " + ("fixedend" if b["explicit_end"] else "auto")
            if b["hidden"]:
                cls += " hiddenblk"
            # 애플 캘린더 스타일: 틴트 배경(불투명 — 겹침/선택 시 뒤가 비치지 않게) + 좌측 컬러 바 + 진한 컬러 텍스트
            style = (
                f"background:{_tint_solid(color, 0.16)};color:{_shade(color)};"
                f"border-left:3px solid {color};"
            )
            if b["explicit_end"]:
                style += f"border-bottom:2px solid {color};"
            else:
                style += f"border-bottom:1px dashed {_tint(color, 0.6)};"
            start_label = (b["orig_start"] or b["start"]).strftime("%H:%M")
            prefix = "↪ " if b["carry"] else ""
            end_str = f'-{b["explicit_hhmm"]}' if b["explicit_end"] else ""
            suffix = " · 진행 중" if b["ongoing"] else ""
            label = f'{prefix}{start_label}{end_str} {html_lib.escape(b["text"])}{suffix}'
            data_attrs = (
                f' data-id="{b["id"]}"'
                f' data-end="{b["explicit_hhmm"]}"'
                f' data-category="{html_lib.escape(b["category"] or "", quote=True)}"'
                f' data-text="{html_lib.escape(b["text"], quote=True)}"'
                f' data-start="{start_label}"'
                f' data-hidden="{1 if b["hidden"] else 0}"'
            )
        col, ncols = layouts[bi]
        if ncols > 1:
            w = 100.0 / ncols
            style += f"left:calc({col * w:.2f}% + 2px);width:calc({w:.2f}% - 4px);right:auto;"
        parts.append(
            f'<div class="{cls}"{data_attrs} style="top:calc({start_min:.1f} * var(--ppm));'
            f'height:calc({max(dur, 1):.1f} * var(--ppm));'
            f'min-height:calc({max(dur, 1):.1f} * var(--ppm));{style}" title="{label}">'
            f'<span class="bt">{label}</span></div>'
        )
    return f'<div class="day"><div class="canvas" data-date="{date}">{"".join(parts)}</div></div>'


def _fmt_min(m: int) -> str:
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def _chip(name: str, minutes: Optional[int]) -> str:
    color = UNCATEGORIZED_COLOR if name in (UNCATEGORIZED, "기록 없음") else category_color(name)
    if minutes is None:
        body = html_lib.escape(name)
    else:
        t = f"{minutes // 60}시간 {minutes % 60}분" if minutes >= 60 else f"{minutes}분"
        body = f"{html_lib.escape(name)} {t}"
    return (
        f'<span class="chip" style="background:{color}1f;color:{color};'
        f'border-color:{color}55">{body}</span>'
    )


def render_timeline(start_date: str, days: int, show_hidden: bool) -> str:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    prev_date = (start - timedelta(days=days)).strftime("%Y-%m-%d")
    next_date = (start + timedelta(days=days)).strftime("%Y-%m-%d")
    today = now_kst().strftime("%Y-%m-%d")
    hq = "&hidden=1" if show_hidden else ""

    per_day = {d: build_blocks(d) for d in dates}

    totals: dict[str, int] = {}
    for blocks in per_day.values():
        for b in blocks:
            if b["empty"] or b["hidden"]:
                continue
            key = b["category"] or UNCATEGORIZED
            totals[key] = totals.get(key, 0) + _visible_minutes(b)
    totals_html = "".join(
        _chip(k, v) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
    ) or _chip("기록 없음", None)

    # 오늘이 보이면 00:00~현재의 빈 시간을 실시간 칩으로 표시 (JS가 1분마다 갱신)
    empty_chip = ""
    if today in per_day:
        now = now_kst()
        elapsed = int((now - now.replace(hour=0, minute=0, second=0, microsecond=0))
                      .total_seconds() // 60)
        covered = min(union_minutes(per_day[today], clip_end=now), elapsed)
        empty_min = max(0, elapsed - covered)
        ongoing = any(b["ongoing"] for b in per_day[today])
        cov_pct = round(covered * 100 / elapsed) if elapsed else 100
        empty_chip = (
            f'<span class="chip emptychip" id="emptychip" data-covered="{covered}" '
            f'data-ongoing="{1 if ongoing else 0}" style="--p:{cov_pct}%">'
            f"빈 시간 {_fmt_min(empty_min)} · 커버리지 {cov_pct}%</span>"
        )

    dayheads_html = "".join(
        f'<a class="dayhead{" today" if d == today else ""}" href="/timeline?date={d}&days=1{hq}">'
        f'<span class="wd">{WEEKDAYS[datetime.strptime(d, "%Y-%m-%d").weekday()]}</span>'
        f'<span class="dn">{int(d[8:10])}</span></a>'
        for d in dates
    )
    columns_html = "".join(render_day_column(d, per_day[d], show_hidden) for d in dates)
    hours_html = "".join(
        f'<div class="hour" style="top:calc({(h - DAY_START_HOUR) * 60} * var(--ppm))"><span class="bt">{h:02d}</span></div>'
        for h in range(DAY_START_HOUR, DAY_END_HOUR + 1)
    )
    days_options = "".join(
        f'<option value="{n}"{" selected" if n == days else ""}>{n}일</option>' for n in range(1, 8)
    )
    wheel_h_items = "".join(f'<div class="wi">{h:02d}</div>' for h in range(24))
    wheel_m_items = "".join(f'<div class="wi">{m:02d}</div>' for m in range(60))
    cat_options = "".join(f'<option value="{c}">{c}</option>' for c in CATEGORIES)
    hidden_toggle = (
        f'<a class="hlink" href="/timeline?date={start_date}&days={days}">숨김 닫기</a>'
        if show_hidden else
        f'<a class="hlink" href="/timeline?date={start_date}&days={days}&hidden=1">숨김 보기</a>'
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{start_date} 타임라인</title>
<link rel="manifest" href="/static/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<meta name="theme-color" content="#2563eb">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="활동 로그">
<style>
  :root {{ --ppm: 1.6px; --accent: #007aff; --red: #ff3b30; --text: #1c1c1e;
           --muted: #8e8e93; --line: rgba(60,60,67,.13); --fill: rgba(120,120,128,.12); }}
  * {{ box-sizing: border-box; }}
  /* 줌으로 문서 높이가 바뀔 때 브라우저의 자동 스크롤 보정(scroll anchoring)이
     우리의 수동 보정과 충돌해 화면이 튀므로 비활성화. 당겨서 새로고침도 차단 */
  html {{ overflow-anchor: none; overscroll-behavior-y: contain; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
                       "Apple SD Gothic Neo", "Pretendard", "Noto Sans KR", "Segoe UI", sans-serif;
          margin: 0; background: #fff; color: var(--text);
          -webkit-font-smoothing: antialiased; letter-spacing: -0.015em;
          touch-action: pan-y; }}
  /* iOS 네비게이션 바: 반투명 + 블러 */
  .topbar {{ position: sticky; top: 0; z-index: 20;
             background: rgba(249,249,251,.82);
             -webkit-backdrop-filter: blur(20px) saturate(180%);
             backdrop-filter: blur(20px) saturate(180%);
             border-bottom: 0.5px solid var(--line);
             padding: 8px 10px 0; }}
  .controls {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
  .controls .nav {{ text-decoration: none; color: var(--accent); font-size: 17px; padding: 6px 8px; }}
  .controls .nav:active {{ opacity: .35; }}
  .controls input[type=date], .controls select {{
    height: 34px; font-size: 14px; border: none; border-radius: 10px;
    background: var(--fill); color: var(--text); padding: 0 10px;
    -webkit-appearance: none; appearance: none; }}
  .controls input[type=date] {{ font-variant-numeric: tabular-nums; font-weight: 500; }}
  .controls .todaybtn {{ height: 34px; padding: 0 11px; border: none; border-radius: 10px;
                         background: var(--fill); color: var(--accent);
                         font-size: 14px; font-weight: 600; text-decoration: none;
                         cursor: pointer; display: inline-flex; align-items: center; }}
  .controls .todaybtn:active {{ opacity: .5; }}
  .controls .hlink {{ font-size: 12px; color: var(--muted); text-decoration: none; padding: 4px; }}
  .zoom {{ margin-left: auto; display: flex; gap: 5px; }}
  .zoom button {{ width: 34px; height: 34px; padding: 0; border: none; border-radius: 50%;
                  background: var(--fill); color: var(--accent); font-size: 17px;
                  font-weight: 600; cursor: pointer; }}
  .zoom button:active {{ opacity: .5; }}
  .quickrow {{ display: flex; gap: 6px; padding: 7px 0 0; }}
  #quickin {{ flex: 1; min-width: 0; height: 36px; font-size: 15px; border: none;
              border-radius: 10px; background: var(--fill); color: var(--text);
              padding: 0 12px; }}
  #quickin:focus {{ outline: 2px solid rgba(0,122,255,.5); }}
  #quickbtn {{ height: 36px; padding: 0 15px; border: none; border-radius: 10px;
               background: var(--accent); color: #fff; font-size: 14px; font-weight: 600;
               cursor: pointer; }}
  #quickbtn:active {{ opacity: .5; }}
  #quickbtn:disabled {{ opacity: .4; }}
  .totals {{ padding: 8px 0; display: flex; flex-wrap: wrap; gap: 5px; }}
  .chip {{ border-radius: 999px; padding: 3.5px 11px; font-size: 12px; font-weight: 600;
           white-space: nowrap; border: none; letter-spacing: -0.01em; }}
  /* 커버리지 프로그레스 캡슐: --p 위치까지 파란 틴트로 차오름 */
  .chip.emptychip {{ background: linear-gradient(90deg,
                       rgba(0,122,255,.28) var(--p, 0%), rgba(120,120,128,.14) var(--p, 0%));
                     color: #3c3c43; font-variant-numeric: tabular-nums;
                     box-shadow: inset 0 0 0 1px rgba(60,60,67,.08); }}
  .dayheads {{ display: flex; margin-left: 44px; }}
  .dayhead {{ flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center;
              gap: 1px; padding: 4px 0 6px; text-decoration: none; }}
  .dayhead .wd {{ font-size: 10.5px; font-weight: 600; color: var(--muted);
                  text-transform: uppercase; letter-spacing: .02em; }}
  .dayhead .dn {{ font-size: 16px; font-weight: 600; color: var(--text);
                  width: 27px; height: 27px; line-height: 27px; text-align: center;
                  border-radius: 50%; font-variant-numeric: tabular-nums; }}
  .dayhead.today .wd {{ color: var(--red); }}
  .dayhead.today .dn {{ background: var(--red); color: #fff; font-weight: 700; }}
  .layout {{ display: flex; touch-action: pan-y; }}
  .axis {{ width: 44px; flex: none; position: relative; height: calc({TOTAL_MIN} * var(--ppm)); }}
  .hour {{ position: absolute; right: 6px; transform: translateY(-50%); font-size: 11px;
           color: var(--muted); font-variant-numeric: tabular-nums; }}
  .daycols {{ display: flex; flex: 1; min-width: 0; }}
  .day {{ flex: 1; min-width: 0; border-left: 1px solid var(--line); }}
  .canvas {{ position: relative; height: calc({TOTAL_MIN} * var(--ppm));
             background-image: linear-gradient(to bottom, rgba(60,60,67,.08) 1px, transparent 1px);
             background-size: 100% calc(60 * var(--ppm));
             contain: layout paint; }}
  .block {{ position: absolute; left: 2px; right: 2px; border-radius: 7px; padding: 2px 7px;
            font-size: 12px; line-height: 1.35; font-weight: 600; overflow: hidden; z-index: 1;
            transition: box-shadow .18s ease; }}
  .block.empty {{ background: #c9c9d0; color: #47474f;
                  border-left: 3px solid #9f9fa8; font-weight: 500; }}
  .block.hiddenblk {{ opacity: .45; }}
  .block.focus {{ height: auto !important; min-height: 22px; z-index: 5;
                  left: 2px !important; right: 2px !important; width: auto !important;
                  outline: none;
                  box-shadow: 0 0 0 2.5px rgba(0,122,255,.55), 0 10px 28px rgba(0,0,0,.16); }}
  .block.focus .bt {{ white-space: normal; }}
  @media (hover: hover) and (pointer: fine) {{
    .block:hover {{ height: auto !important; min-height: 22px; z-index: 5;
                    left: 2px !important; right: 2px !important; width: auto !important;
                    outline: none;
                    box-shadow: 0 0 0 2.5px rgba(0,122,255,.55), 0 10px 28px rgba(0,0,0,.16); }}
    .block:hover .bt {{ white-space: normal; }}
  }}
  /* 종료 확정/자동연장 구분은 인라인 스타일(카테고리 색)로 처리 */
  .bt {{ display: block; transform-origin: 0 0;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  html.pinching .bt {{ transform: scaleY(var(--isy, 1)); }}
  .nowline {{ position: absolute; left: 0; right: 0; height: 0; border-top: 2px solid var(--red); z-index: 2; }}
  .nowline::before {{ content: ""; position: absolute; left: -4px; top: -5px; width: 8px; height: 8px;
                      border-radius: 50%; background: var(--red); }}
  /* iOS 시트 스타일 편집 패널 */
  #editbar {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 30;
              background: rgba(249,249,251,.94);
              -webkit-backdrop-filter: blur(20px) saturate(180%);
              backdrop-filter: blur(20px) saturate(180%);
              border-radius: 14px 14px 0 0;
              box-shadow: 0 -8px 32px rgba(0,0,0,.14);
              display: flex; flex-direction: column; gap: 8px; padding: 7px 14px 12px;
              padding-bottom: calc(12px + env(safe-area-inset-bottom)); }}
  #editbar[hidden] {{ display: none; }}
  #editbar::before {{ content: ""; align-self: center; width: 36px; height: 5px;
                      border-radius: 3px; background: rgba(60,60,67,.25); }}
  @media (prefers-reduced-motion: no-preference) {{
    #editbar:not([hidden]) {{ animation: sheetup .28s cubic-bezier(.2,.9,.3,1); }}
  }}
  @keyframes sheetup {{ from {{ transform: translateY(60%); opacity: .4; }}
                        to {{ transform: none; opacity: 1; }} }}
  .ebrow {{ display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }}
  #edittext {{ flex: 1; min-width: 0; font-size: 15px; padding: 9px 11px; border: none;
               border-radius: 10px; background: var(--fill); color: var(--text); }}
  #edittext:focus {{ outline: 2px solid rgba(0,122,255,.5); }}
  #editcat {{ flex: 1; min-width: 108px; height: 46px; font-size: 14px; border: none;
              border-radius: 10px; background: var(--fill); color: var(--text); padding: 0 10px;
              -webkit-appearance: none; appearance: none; }}
  /* 시작 → 종료를 하나의 그룹으로 */
  #timerange {{ display: flex; align-items: center; background: var(--fill);
                border-radius: 10px; padding: 5px 4px; gap: 2px; }}
  .tcell {{ display: flex; flex-direction: column; align-items: center; gap: 0;
            padding: 0 10px; }}
  .tlabel {{ font-size: 10px; font-weight: 600; color: var(--muted);
             text-transform: uppercase; letter-spacing: .04em; }}
  #editstart {{ font-size: 16px; font-weight: 600; color: var(--accent);
                font-variant-numeric: tabular-nums; line-height: 1.3; }}
  .tarrow {{ color: var(--muted); font-size: 14px; padding-bottom: 2px; }}
  #startcell, #endcell {{ cursor: pointer; }}
  #enddisp {{ font-size: 16px; font-weight: 600; color: var(--accent);
              font-variant-numeric: tabular-nums; line-height: 1.3; }}
  /* iOS 시계 휠 피커: 스크롤 스냅 드럼 2개 + 중앙 하이라이트 밴드 */
  #wheelbox {{ display: flex; flex-direction: column; gap: 4px; }}
  #wheelbox[hidden] {{ display: none; }}
  .wheels {{ position: relative; display: flex; justify-content: center; gap: 2px; height: 180px; }}
  .wheelband {{ position: absolute; left: 22%; right: 22%; top: 72px; height: 36px;
                border-radius: 9px; background: var(--fill); }}
  .wheel {{ position: relative; z-index: 1; height: 180px; overflow-y: scroll; width: 66px;
            scroll-snap-type: y mandatory; padding: 72px 0; text-align: center;
            -webkit-overflow-scrolling: touch; scrollbar-width: none;
            -webkit-mask-image: linear-gradient(transparent, #000 28%, #000 72%, transparent);
            mask-image: linear-gradient(transparent, #000 28%, #000 72%, transparent); }}
  .wheel::-webkit-scrollbar {{ display: none; }}
  .wi {{ height: 36px; line-height: 36px; font-size: 21px; font-weight: 600;
         color: var(--text); font-variant-numeric: tabular-nums; scroll-snap-align: center;
         cursor: pointer; }}
  .wcolon {{ z-index: 1; line-height: 176px; font-size: 20px; font-weight: 700; color: var(--text); }}
  #editbar #noend {{ align-self: center; background: none; color: var(--muted);
                     font-size: 13px; padding: 4px 12px; font-weight: 500; }}
  #editbar button {{ font-size: 14px; padding: 9px 13px; border: none; border-radius: 10px;
                     background: var(--fill); white-space: nowrap; cursor: pointer;
                     color: var(--accent); font-weight: 600; }}
  #editbar button:active {{ opacity: .5; }}
  #editbar #endsave {{ background: var(--accent); color: #fff; flex: 1; }}
  #hidebtn {{ color: var(--red); background: rgba(255,59,48,.1); }}
</style>
</head>
<body>
<div class="topbar">
  <div class="controls">
    <a class="nav" href="/timeline?date={prev_date}&days={days}{hq}">◀</a>
    <input type="date" id="datepick" value="{start_date}">
    <a class="nav" href="/timeline?date={next_date}&days={days}{hq}">▶</a>
    <select id="dayspick">{days_options}</select>
    <a class="todaybtn" href="/timeline?date={today}&days={days}{hq}">오늘</a>
    <button class="todaybtn" id="gotonow">현재</button>
    <a class="todaybtn" href="/stats">통계</a>
    {hidden_toggle}
    <div class="zoom">
      <button id="zoomout" aria-label="축소">−</button>
      <button id="zoomin" aria-label="확대">＋</button>
    </div>
  </div>
  <div class="quickrow">
    <input type="text" id="quickin" placeholder="무엇을 하고 있나요? · 종료는 '~끝'" autocomplete="off">
    <button id="quickbtn">기록</button>
  </div>
  <div class="totals">{empty_chip}{totals_html}</div>
  <div class="dayheads">{dayheads_html}</div>
</div>
<div class="layout">
  <div class="axis">{hours_html}</div>
  <div class="daycols">{columns_html}</div>
</div>
<div id="editbar" hidden>
  <div class="ebrow">
    <input type="text" id="edittext" placeholder="텍스트 수정">
  </div>
  <div class="ebrow">
    <div id="timerange">
      <div class="tcell" id="startcell"><span class="tlabel">시작</span><span id="editstart">–</span></div>
      <div class="tarrow">→</div>
      <div class="tcell" id="endcell"><span class="tlabel">종료</span>
        <span id="enddisp">–</span>
      </div>
    </div>
    <select id="editcat">
      <option value="">자동 재분류(다음 배치)</option>
      {cat_options}
    </select>
  </div>
  <div id="wheelbox" hidden>
    <div class="wheels">
      <div class="wheelband"></div>
      <div class="wheel" id="wh">{wheel_h_items}</div>
      <div class="wcolon">:</div>
      <div class="wheel" id="wm">{wheel_m_items}</div>
    </div>
    <button id="noend">종료 시각 없음</button>
  </div>
  <div class="ebrow">
    <button id="endsave">저장</button>
    <button id="hidebtn">숨김</button>
    <button id="closebtn">✕</button>
  </div>
</div>
<script>
(function () {{
  var DAYS = {days};
  var HQ = '{hq}';
  var root = document.documentElement;
  var MIN = 0.4, MAX = 20;
  var ppm = Math.min(MAX, Math.max(MIN, parseFloat(localStorage.getItem('ppm')) || 1.6));

  function apply() {{ root.style.setProperty('--ppm', ppm + 'px'); }}
  apply();

  var layout = document.querySelector('.layout');

  function zoomAt(factor, clientY) {{
    var old = ppm;
    ppm = Math.min(MAX, Math.max(MIN, ppm * factor));
    if (ppm === old) return;
    var rect = layout.getBoundingClientRect();
    var y = clientY - rect.top;
    var target = window.scrollY + y * (ppm / old) - y;
    apply();
    localStorage.setItem('ppm', ppm);
    window.scrollTo(0, target);
  }}

  document.getElementById('zoomin').addEventListener('click', function () {{
    zoomAt(1.3, window.innerHeight / 2);
  }});
  document.getElementById('zoomout').addEventListener('click', function () {{
    zoomAt(1 / 1.3, window.innerHeight / 2);
  }});

  layout.addEventListener('wheel', function (e) {{
    if (!e.ctrlKey) return;
    e.preventDefault();
    zoomAt(e.deltaY < 0 ? 1.022 : 1 / 1.022, e.clientY);
  }}, {{ passive: false }});

  // 모바일 핀치: 제스처 중 GPU transform(scaleY)만 갱신, 손 뗄 때 1회 커밋
  var PINCH_DAMP = 0.5;
  var pinch = null, lastPinchEnd = 0, pinchSeq = false;
  var rafId = null, pendingScale = null;
  var lastScrollAt = 0;
  window.addEventListener('scroll', function () {{ lastScrollAt = performance.now(); }}, {{ passive: true }});

  function touchDist(t) {{
    return Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  }}

  function commitPinch() {{
    if (!pinch) return;
    var s = pendingScale || 1;
    ppm = Math.min(MAX, Math.max(MIN, pinch.startPpm * s));
    layout.style.transform = '';
    layout.style.willChange = '';
    root.classList.remove('pinching');
    root.style.removeProperty('--isy');
    apply();
    window.scrollTo(0, Math.max(0, pinch.layoutTop + pinch.anchorPx * (ppm / pinch.startPpm) - pinch.anchorClientY));
    localStorage.setItem('ppm', ppm);
    pinch = null;
    pendingScale = null;
    if (rafId) {{ cancelAnimationFrame(rafId); rafId = null; }}
  }}

  function initPinch(e) {{
    if (pinch) commitPinch();
    var d = touchDist(e.touches);
    if (d < 10) return;
    var rect = layout.getBoundingClientRect();
    var midY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    layout.style.transformOrigin = '0 0';
    layout.style.willChange = 'transform';
    root.classList.add('pinching');
    pinch = {{
      startDist: d,
      startPpm: ppm,
      anchorPx: midY - rect.top,
      anchorClientY: midY,
      layoutTop: rect.top + window.scrollY
    }};
    pendingScale = 1;
    pinchSeq = true;
  }}

  layout.addEventListener('touchstart', function (e) {{
    if (e.touches.length !== 2) return;
    if (performance.now() - lastScrollAt < 100) {{
      var y = window.scrollY;
      root.style.overflow = 'hidden';
      void root.offsetHeight;
      root.style.overflow = '';
      window.scrollTo(0, y);
    }}
    if (!e.cancelable) return;
    e.preventDefault();
    initPinch(e);
  }}, {{ passive: false }});

  layout.addEventListener('touchmove', function (e) {{
    if (pinchSeq && e.cancelable && (e.touches.length !== 2 || !pinch)) {{
      e.preventDefault();
      return;
    }}
    if (e.touches.length !== 2) return;
    if (!e.cancelable) {{ if (pinch) commitPinch(); return; }}
    if (!pinch) {{ initPinch(e); if (!pinch) return; }}
    e.preventDefault();
    var ratio = 1 + (touchDist(e.touches) / pinch.startDist - 1) * PINCH_DAMP;
    var targetPpm = Math.min(MAX, Math.max(MIN, pinch.startPpm * ratio));
    pendingScale = targetPpm / pinch.startPpm;
    if (!rafId) {{
      rafId = requestAnimationFrame(function () {{
        rafId = null;
        if (!pinch || pendingScale === null) return;
        var ty = pinch.anchorPx * (1 - pendingScale);
        layout.style.transform = 'translateY(' + ty + 'px) scaleY(' + pendingScale + ')';
        root.style.setProperty('--isy', 1 / pendingScale);
      }});
    }}
  }}, {{ passive: false }});

  ['touchend', 'touchcancel'].forEach(function (t) {{
    layout.addEventListener(t, function (e) {{
      if (pinch) {{
        if (e.touches.length === 2) {{ initPinch(e); return; }}
        commitPinch();
        lastPinchEnd = Date.now();
      }}
      if (e.touches.length === 0) pinchSeq = false;
    }});
  }});

  // ---------- 편집 패널 ----------
  var editbar = document.getElementById('editbar');
  var selected = null;
  var dirty = {{ text: false, cat: false, end: false }};
  var edittext = document.getElementById('edittext');
  var editcat = document.getElementById('editcat');

  // ---- iOS 스타일 시계 휠 피커 (시작/종료 겸용) ----
  var editstart = document.getElementById('editstart');
  var enddisp = document.getElementById('enddisp');
  var wheelbox = document.getElementById('wheelbox');
  var noendBtn = document.getElementById('noend');
  var wh = document.getElementById('wh');
  var wm = document.getElementById('wm');
  var WI = 36;                 // 휠 항목 높이(px)
  var startVal = '';           // 'HH:MM'
  var endVal = '';             // '' = 종료 미지정, 'HH:MM'
  var endNextDay = false;      // 종료가 익일인지 (예: 수면 23:00 → 익일 07:00)
  var editTarget = 'end';      // 휠이 편집 중인 대상: 'start' | 'end'
  var cur = {{ h: 0, m: 0 }};

  function pad2(n) {{ return String(n).padStart(2, '0'); }}
  function toMin(hhmm) {{
    return parseInt(hhmm.slice(0, 2), 10) * 60 + parseInt(hhmm.slice(3, 5), 10);
  }}
  function addMin(hhmm, delta) {{
    var t = Math.max(0, Math.min(toMin(hhmm) + delta, 23 * 60 + 59));
    return pad2(Math.floor(t / 60)) + ':' + pad2(t % 60);
  }}
  function setStartVal(v) {{
    startVal = v;
    editstart.textContent = v || '–';
  }}
  function setEndVal(v) {{
    endVal = v;
    if (!v) endNextDay = false;
    enddisp.textContent = v ? ((endNextDay ? '익일 ' : '') + v) : '–';
  }}
  function positionWheels(v) {{
    cur.h = parseInt(v.slice(0, 2), 10);
    cur.m = parseInt(v.slice(3, 5), 10);
    wh.scrollTop = cur.h * WI;
    wm.scrollTop = cur.m * WI;
  }}
  function revertEnd() {{
    // 잘못된 선택을 마지막 유효 값(없으면 시작+1분)으로 되돌림
    var back = endVal || (startVal ? addMin(startVal, 1) : '00:00');
    setEndVal(back);
    positionWheels(back);
  }}
  function bindWheel(el, part) {{
    var t = null;
    el.addEventListener('scroll', function () {{
      if (t) clearTimeout(t);
      t = setTimeout(function () {{
        var idx = Math.max(0, Math.min(Math.round(el.scrollTop / WI), el.children.length - 1));
        var curVal = editTarget === 'end' ? endVal : startVal;
        if (cur[part] === idx && curVal) return;  // 위치 이동만으로는 변경 아님
        var cand = {{ h: cur.h, m: cur.m }};
        cand[part] = idx;
        var candVal = pad2(cand.h) + ':' + pad2(cand.m);
        if (editTarget === 'end') {{
          if (startVal && candVal === startVal) {{
            alert('종료가 시작 시각과 같아요');
            revertEnd();
            return;
          }}
          var nd = !!(startVal && candVal < startVal);  // 시작 이전 시각 = 익일 종료로 해석
          if (nd) {{
            if (24 * 60 - toMin(startVal) + toMin(candVal) > 18 * 60) {{
              alert('18시간을 넘는 활동은 저장할 수 없어요');
              revertEnd();
              return;
            }}
            if (!confirm('종료를 다음 날 ' + candVal + '(익일)로 저장할까요?')) {{
              revertEnd();
              return;
            }}
          }}
          cur[part] = idx;
          endNextDay = nd;
          setEndVal(candVal);
          dirty.end = true;
        }} else {{
          if (endVal) {{
            var bad = '';
            if (endNextDay) {{
              if (24 * 60 - toMin(candVal) + toMin(endVal) > 18 * 60) {{
                bad = '18시간을 넘는 활동은 저장할 수 없어요';
              }}
            }} else if (candVal >= endVal) {{
              bad = '시작 시각은 종료(' + endVal + ') 이전이어야 해요';
            }}
            if (bad) {{
              alert(bad);
              var back2 = startVal || addMin(endVal, -1);
              setStartVal(back2);
              positionWheels(back2);
              return;
            }}
          }}
          cur[part] = idx;
          setStartVal(candVal);
          dirty.start = true;
        }}
      }}, 130);
    }});
    el.addEventListener('click', function (e) {{
      var wi = e.target.closest ? e.target.closest('.wi') : null;
      if (wi) el.scrollTo({{ top: Array.prototype.indexOf.call(el.children, wi) * WI,
                             behavior: 'smooth' }});
    }});
  }}
  bindWheel(wh, 'h');
  bindWheel(wm, 'm');

  function openWheel(target) {{
    if (!wheelbox.hidden && editTarget === target) {{ wheelbox.hidden = true; return; }}
    editTarget = target;
    noendBtn.hidden = target !== 'end';  // '종료 시각 없음'은 종료 편집일 때만
    wheelbox.hidden = false;
    var init;
    if (target === 'end') {{
      // 미지정이면 현재 시각(단, 시작 이전이면 시작+1분)에서 시작 — 휠 정렬되며 값 선택 (iOS와 동일)
      init = endVal;
      if (!init) {{
        var nowStr = new Date().toTimeString().slice(0, 5);
        init = (startVal && nowStr <= startVal) ? addMin(startVal, 1) : nowStr;
      }}
    }} else {{
      init = startVal || new Date().toTimeString().slice(0, 5);
    }}
    positionWheels(init);
  }}
  document.getElementById('endcell').addEventListener('click', function () {{ openWheel('end'); }});
  document.getElementById('startcell').addEventListener('click', function () {{ openWheel('start'); }});
  noendBtn.addEventListener('click', function () {{
    setEndVal('');
    dirty.end = true;
    wheelbox.hidden = true;
  }});

  edittext.addEventListener('input', function () {{ dirty.text = true; }});
  edittext.addEventListener('keydown', function (e) {{
    // 엔터로 바로 저장 (한글 IME 조합 확정 엔터는 무시 — 중복 저장 방지)
    if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    saveAll();
  }});
  editcat.addEventListener('change', function () {{ dirty.cat = true; saveAll(); }});  // 선택 즉시 저장

  function openEdit(b) {{
    selected = b;
    dirty = {{ text: false, cat: false, end: false, start: false }};
    setStartVal(b.dataset.start || '');
    edittext.value = b.dataset.text || '';
    editcat.value = b.dataset.category || '';
    // 저장된 종료가 시작보다 이른 HH:MM이면 익일 종료 (예: 23:00 → 07:00)
    endNextDay = !!(b.dataset.end && b.dataset.start && b.dataset.end < b.dataset.start);
    setEndVal(b.dataset.end || '');
    wheelbox.hidden = true;
    document.getElementById('hidebtn').textContent = b.dataset.hidden === '1' ? '숨김 해제' : '숨김';
    editbar.hidden = false;
    document.body.style.paddingBottom = '140px';
  }}

  function closeEdit() {{
    selected = null;
    editbar.hidden = true;
    document.body.style.paddingBottom = '';
    document.querySelectorAll('.block.focus').forEach(function (x) {{ x.classList.remove('focus'); }});
  }}

  document.addEventListener('click', function (e) {{
    if (Date.now() - lastPinchEnd < 400) return;
    if (e.target.closest && e.target.closest('#editbar')) return;
    var b = e.target.closest ? e.target.closest('.block') : null;
    document.querySelectorAll('.block.focus').forEach(function (x) {{
      if (x !== b) x.classList.remove('focus');
    }});
    if (b) b.classList.toggle('focus');
    if (b && b.classList.contains('focus') && b.dataset.id) {{
      openEdit(b);
    }} else {{
      closeEdit();
    }}
  }});

  function api(path, body) {{
    return fetch(path, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-Requested-With': 'timeline' }},
      body: JSON.stringify(body)
    }}).then(function (r) {{
      if (r.ok) return;
      return r.json().then(function (j) {{ throw new Error(j.error || ('저장 실패 ' + r.status)); }},
                           function () {{ throw new Error('저장 실패 ' + r.status); }});
    }});
  }}

  function saveAll() {{
    if (!selected) return;
    var id = selected.dataset.id;
    var seq = Promise.resolve();
    if (dirty.text) {{
      var t = edittext.value.trim();
      seq = seq.then(function () {{ return api('/events/' + id + '/edit', {{ text: t || null }}); }});
    }}
    if (dirty.cat) {{
      var c = editcat.value;
      seq = seq.then(function () {{ return api('/events/' + id + '/category', {{ category: c || null }}); }});
    }}
    if (dirty.start) {{
      var sv = startVal;
      if (!sv) {{ alert('시작 시각이 비어 있어요'); return; }}
      if (endVal && !endNextDay && endVal <= sv) {{
        alert('시작 시각은 종료(' + endVal + ') 이전이어야 해요');
        return;
      }}
      seq = seq.then(function () {{ return api('/events/' + id + '/start', {{ start: sv }}); }});
    }}
    if (dirty.end) {{
      var v = endVal;
      if (v) {{
        if (startVal && v === startVal) {{
          alert('종료가 시작 시각과 같아요');
          return;
        }}
        if (startVal && v < startVal && !endNextDay) {{
          alert('종료 시각은 시작(' + startVal + ') 이후여야 해요');
          return;
        }}
        seq = seq.then(function () {{ return api('/events/' + id + '/end', {{ end: v }}); }});
      }} else {{
        seq = seq.then(function () {{ return api('/events/' + id + '/end', {{ end: null }}); }});
      }}
    }}
    if (!dirty.text && !dirty.cat && !dirty.end && !dirty.start) {{ closeEdit(); return; }}
    seq.then(function () {{ location.reload(); }})
       .catch(function (e) {{ alert(e.message); }});
  }}
  document.getElementById('endsave').addEventListener('click', saveAll);

  document.getElementById('hidebtn').addEventListener('click', function () {{
    if (!selected) return;
    var hiding = selected.dataset.hidden !== '1';
    var msg = hiding ? '이 기록을 숨길까요? 타임라인·통계에서 제외됩니다.\\n(숨김 보기에서 되돌릴 수 있어요)'
                     : '숨김을 해제할까요?';
    if (!confirm(msg)) return;
    api('/events/' + selected.dataset.id + '/hide', {{ hidden: hiding }})
      .then(function () {{ location.reload(); }})
      .catch(function (e) {{ alert(e.message); }});
  }});

  document.getElementById('closebtn').addEventListener('click', closeEdit);

  document.getElementById('datepick').addEventListener('change', function () {{
    if (this.value) location.href = '/timeline?date=' + this.value + '&days=' + DAYS + HQ;
  }});
  document.getElementById('dayspick').addEventListener('change', function () {{
    location.href = '/timeline?date={start_date}&days=' + this.value + HQ;
  }});

  function todayStr() {{
    var now = new Date();
    return now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' +
           String(now.getDate()).padStart(2, '0');
  }}
  function scrollToMin(min) {{
    var rect = layout.getBoundingClientRect();
    var target = rect.top + window.scrollY + min * ppm - window.innerHeight / 2;
    window.scrollTo({{ top: Math.max(0, target), behavior: 'smooth' }});
  }}
  document.getElementById('gotonow').addEventListener('click', function () {{
    var ds = todayStr();
    if (document.querySelector('.canvas[data-date="' + ds + '"]')) {{
      var now = new Date();
      scrollToMin((now.getHours() - {DAY_START_HOUR}) * 60 + now.getMinutes());
    }} else {{
      location.href = '/timeline?date=' + ds + '&days=' + DAYS + HQ + '#now';
    }}
  }});

  // 빠른 입력칸: 어느 기기든 브라우저에서 바로 기록 (마커 '~끝'도 동일 동작)
  (function quickLog() {{
    var qi = document.getElementById('quickin');
    var qb = document.getElementById('quickbtn');
    function send() {{
      if (qb.disabled) return;  // 이중 호출 방지
      var t = qi.value.trim();
      if (!t) return;
      qb.disabled = true;
      fetch('/log', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json', 'X-Requested-With': 'timeline' }},
        body: JSON.stringify({{ text: t, source: 'web' }})
      }}).then(function (r) {{
        if (!r.ok) throw new Error('기록 실패 ' + r.status);
        var ds = todayStr();
        if (document.querySelector('.canvas[data-date="' + ds + '"]')) location.reload();
        else location.href = '/timeline?date=' + ds + '&days=' + DAYS + HQ;
      }}).catch(function (e) {{ alert(e.message); qb.disabled = false; }});
    }}
    qi.addEventListener('keydown', function (e) {{
      // 한글 IME 조합 중 엔터는 keydown이 2번 발생 (조합 확정 + 실제 입력) → 중복 전송 방지
      if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      send();
    }});
    qb.addEventListener('click', send);
  }})();

  // 현재 시각 표시선 (오늘 컬럼에만)
  function nowline() {{
    document.querySelectorAll('.nowline').forEach(function (n) {{ n.remove(); }});
    var now = new Date();
    var c = document.querySelector('.canvas[data-date="' + todayStr() + '"]');
    if (!c) return;
    var min = (now.getHours() - {DAY_START_HOUR}) * 60 + now.getMinutes();
    if (min < 0 || min > {TOTAL_MIN}) return;
    var l = document.createElement('div');
    l.className = 'nowline';
    l.style.top = 'calc(' + min + ' * var(--ppm))';
    c.appendChild(l);
  }}
  nowline();
  setInterval(nowline, 60000);

  // 실시간 빈 시간 칩: 진행 중 활동이 있으면 빈 시간 고정, 없으면 1분씩 증가
  (function liveEmpty() {{
    var ec = document.getElementById('emptychip');
    if (!ec) return;
    var covered0 = parseInt(ec.dataset.covered, 10);
    var ongoing = ec.dataset.ongoing === '1';
    var n0 = new Date();
    var loadMin = n0.getHours() * 60 + n0.getMinutes();
    function fmtMin(m) {{
      return m >= 60 ? Math.floor(m / 60) + '시간 ' + (m % 60) + '분' : m + '분';
    }}
    function upd() {{
      var n = new Date();
      var elapsed = n.getHours() * 60 + n.getMinutes();
      if (elapsed < loadMin) return;  // 자정 넘김 — 새로고침 전까지 갱신 중단
      var covered = ongoing ? Math.min(covered0 + (elapsed - loadMin), elapsed) : covered0;
      var empty = Math.max(0, elapsed - covered);
      var pct = elapsed ? Math.round(covered * 100 / elapsed) : 100;
      ec.textContent = '빈 시간 ' + fmtMin(empty) + ' · 커버리지 ' + pct + '%';
      ec.style.setProperty('--p', pct + '%');
    }}
    setInterval(upd, 60000);
  }})();

  // 최초 로드 시 보기 좋은 위치로 스크롤 (오늘: 현재-2h, 그 외: 06시)
  (function initialScroll() {{
    var min;
    if (location.hash === '#now' || document.querySelector('.canvas[data-date="' + todayStr() + '"]')) {{
      var now = new Date();
      min = Math.max(0, (now.getHours() - 2) * 60 + now.getMinutes());
    }} else {{
      min = 6 * 60;
    }}
    var rect = layout.getBoundingClientRect();
    window.scrollTo(0, Math.max(0, rect.top + window.scrollY + min * ppm - 60));
  }})();
}})();
</script>
</body>
</html>"""


@app.get("/timeline", response_class=HTMLResponse)
def get_timeline(
    date: Optional[str] = Query(default=None),
    days: int = Query(default=1, ge=1, le=7),
    hidden: int = Query(default=0),
):
    date = date or now_kst().strftime("%Y-%m-%d")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return HTMLResponse("<h1>잘못된 날짜 형식 (YYYY-MM-DD)</h1>", status_code=400)
    return HTMLResponse(
        render_timeline(date, days, bool(hidden)), headers={"Cache-Control": "no-store"}
    )


@app.get("/timeline/fc", response_class=HTMLResponse)
def get_timeline_fc():
    # FullCalendar 기반 타임라인 (실험용 보존)
    html = (ROOT / "app" / "timeline_fc.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/blocks")
def api_blocks(start: str, end: str):
    """FullCalendar용 이벤트 소스. [start, end) 범위의 (일 단위 클리핑된) 블록과 합계."""
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d")
        d1 = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return JSONResponse({"error": "bad date"}, status_code=400)
    n = (d1 - d0).days
    if n < 1 or n > 31:
        return JSONResponse({"error": "bad range"}, status_code=400)

    events, totals = [], {}
    for i in range(n):
        ds = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        for b in build_blocks(ds):
            if b["empty"] or b["hidden"]:
                events.append(
                    {
                        "id": "",
                        "title": "빈 시간",
                        "start": b["start"].isoformat(),
                        "end": b["end"].isoformat(),
                        "classNames": ["evt-empty"],
                        "backgroundColor": "transparent",
                        "borderColor": "#e0e0e0",
                        "textColor": "#80868b",
                        "extendedProps": {"empty": True, "endHHMM": "", "category": ""},
                    }
                )
                continue
            key = b["category"] or UNCATEGORIZED
            totals[key] = totals.get(key, 0) + _visible_minutes(b)
            color = category_color(b["category"])
            prefix = "↪ " if b["carry"] else ""
            events.append(
                {
                    # 이월 블록은 원본과 다른 id로 방출 (FullCalendar가 동일 id를 연동 취급하므로)
                    "id": f'{b["id"]}c' if b["carry"] else str(b["id"]),
                    "title": prefix + b["text"] + (" · 진행 중" if b["ongoing"] else ""),
                    "start": b["start"].isoformat(),
                    "end": b["end"].isoformat(),
                    "classNames": ["evt-fixed" if b["explicit_end"] else "evt-auto"],
                    "backgroundColor": color,
                    "borderColor": color,
                    "textColor": _text_on(color),
                    "extendedProps": {
                        "empty": False,
                        "endHHMM": b["explicit_hhmm"],
                        "category": key,
                    },
                }
            )
    colors = {k: category_color(None if k == UNCATEGORIZED else k) for k in totals}
    return {"events": events, "totals": totals, "colors": colors}


def render_stats(end_date: str) -> str:
    """직전 7일 카테고리 통계. 스택 바(24h 정규화) + 합계 표 + 킬링타임 비율."""
    end = datetime.strptime(end_date, "%Y-%m-%d")
    dates = [(end - timedelta(days=6 - i)).strftime("%Y-%m-%d") for i in range(7)]
    prev_end = (end - timedelta(days=7)).strftime("%Y-%m-%d")
    next_end = (end + timedelta(days=7)).strftime("%Y-%m-%d")
    today = now_kst().strftime("%Y-%m-%d")

    order = CATEGORIES + [UNCATEGORIZED]
    per_day: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for d in dates:
        day_tot: dict[str, int] = {}
        for b in build_blocks(d):
            if b["empty"] or b["hidden"]:
                continue
            key = b["category"] or UNCATEGORIZED
            day_tot[key] = day_tot.get(key, 0) + _visible_minutes(b)
            totals[key] = totals.get(key, 0) + _visible_minutes(b)
        per_day[d] = day_tot

    bar_h = 264  # 24h → 264px (1분 = 0.183px). 미기록 시간은 여백으로 남김
    cols = []
    for d in dates:
        segs = []
        for cat in order:
            m = per_day[d].get(cat, 0)
            if m <= 0:
                continue
            color = category_color(None if cat == UNCATEGORIZED else cat)
            h = max(m * bar_h / TOTAL_MIN, 2)
            t = f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"
            segs.append(
                f'<div class="seg" style="height:{h:.1f}px;background:{color}" '
                f'title="{html_lib.escape(cat)} {t}"></div>'
            )
        wd = WEEKDAYS[datetime.strptime(d, "%Y-%m-%d").weekday()]
        today_cls = " today" if d == today else ""
        cols.append(
            f'<div class="col"><div class="barwrap"><div class="bar">{"".join(segs)}</div></div>'
            f'<a class="daylabel{today_cls}" href="/timeline?date={d}&days=1">'
            f"{int(d[5:7])}/{int(d[8:10])}<br>({wd})</a></div>"
        )

    recorded = sum(totals.values())
    kill = totals.get("킬링타임", 0)
    kill_pct = round(kill * 100 / recorded) if recorded else 0
    kill_t = f"{kill // 60}시간 {kill % 60}분" if kill >= 60 else f"{kill}분"

    rows = []
    for cat, m in sorted(totals.items(), key=lambda kv: -kv[1]):
        color = category_color(None if cat == UNCATEGORIZED else cat)
        t = f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"
        avg = m // 7
        avg_t = f"{avg // 60}시간 {avg % 60}분" if avg >= 60 else f"{avg}분"
        pct = round(m * 100 / recorded) if recorded else 0
        rows.append(
            f'<tr><td><span class="dot" style="background:{color}"></span>'
            f"{html_lib.escape(cat)}</td><td>{t}</td><td>{avg_t}</td><td>{pct}%</td></tr>"
        )
    table_html = "".join(rows) or '<tr><td colspan="4">기록 없음</td></tr>'

    legend_html = "".join(
        _chip(k, None) for k in order if totals.get(k, 0) > 0
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>주간 통계 · {dates[0]} ~ {end_date}</title>
<link rel="manifest" href="/static/manifest.webmanifest">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<meta name="theme-color" content="#2563eb">
<style>
  :root {{ --accent: #007aff; --red: #ff3b30; --text: #1c1c1e; --muted: #8e8e93;
           --line: rgba(60,60,67,.13); --fill: rgba(120,120,128,.12); }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
                       "Apple SD Gothic Neo", "Pretendard", "Noto Sans KR", "Segoe UI", sans-serif;
          margin: 0; background: #f2f2f7; color: var(--text);
          -webkit-font-smoothing: antialiased; letter-spacing: -0.015em; padding-bottom: 28px; }}
  .topbar {{ position: sticky; top: 0; z-index: 10;
             background: rgba(249,249,251,.82);
             -webkit-backdrop-filter: blur(20px) saturate(180%);
             backdrop-filter: blur(20px) saturate(180%);
             border-bottom: 0.5px solid var(--line);
             padding: 10px 14px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .topbar .nav {{ text-decoration: none; color: var(--accent); font-size: 17px; padding: 4px 8px; }}
  .topbar .nav:active {{ opacity: .35; }}
  .topbar h1 {{ font-size: 17px; margin: 0; font-weight: 700; letter-spacing: -0.02em; }}
  .topbar .range {{ font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }}
  .topbar a.tl {{ margin-left: auto; text-decoration: none; color: var(--accent);
                  font-weight: 600; font-size: 14px; background: var(--fill);
                  border-radius: 10px; padding: 7px 12px; }}
  .topbar a.tl:active {{ opacity: .5; }}
  .card {{ margin: 12px 14px 0; background: #fff; border-radius: 14px;
           box-shadow: 0 1px 3px rgba(0,0,0,.05); }}
  .hero {{ padding: 14px 16px; font-size: 14px; color: var(--muted); }}
  .hero .big {{ display: block; font-size: 26px; font-weight: 700; letter-spacing: -0.03em;
                color: {category_color("킬링타임")}; font-variant-numeric: tabular-nums;
                margin-bottom: 2px; }}
  .chart {{ display: flex; gap: 7px; padding: 16px 14px 4px; align-items: flex-end; }}
  .col {{ flex: 1; min-width: 0; text-align: center; }}
  .barwrap {{ height: {bar_h}px; display: flex; align-items: flex-end;
              background: rgba(120,120,128,.07); border-radius: 9px; }}
  .bar {{ width: 100%; display: flex; flex-direction: column-reverse; gap: 2px;
          padding: 2px; }}
  .seg {{ border-radius: 3.5px; }}
  .daylabel {{ display: inline-block; margin-top: 7px; font-size: 12px; color: var(--muted);
               text-decoration: none; line-height: 1.3; font-variant-numeric: tabular-nums; }}
  .daylabel.today {{ color: var(--red); font-weight: 700; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 5px; padding: 12px 14px 14px; }}
  .chip {{ border-radius: 999px; padding: 3.5px 11px; font-size: 12px; font-weight: 600;
           white-space: nowrap; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  .card table {{ margin: 0; }}
  th, td {{ text-align: left; padding: 11px 16px; border-bottom: 0.5px solid var(--line); }}
  tr:last-child td {{ border-bottom: none; }}
  th {{ color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase;
        letter-spacing: .03em; }}
  td:nth-child(n+2), th:nth-child(n+2) {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .dot {{ display: inline-block; width: 11px; height: 11px; border-radius: 4px;
          margin-right: 8px; vertical-align: -1px; }}
</style>
</head>
<body>
<div class="topbar">
  <a class="nav" href="/stats?end={prev_end}">◀</a>
  <div>
    <h1>주간 통계</h1>
    <div class="range">{dates[0]} ~ {end_date}</div>
  </div>
  <a class="nav" href="/stats?end={next_end}">▶</a>
  <a class="tl" href="/timeline">타임라인</a>
</div>
<div class="card hero">
  <span class="big">킬링타임 {kill_t}</span>
  기록 시간의 {kill_pct}% · 총 기록 {recorded // 60}시간 {recorded % 60}분
</div>
<div class="card">
  <div class="chart">{"".join(cols)}</div>
  <div class="legend">{legend_html}</div>
</div>
<div class="card">
<table>
  <tr><th>카테고리</th><th>합계</th><th>일평균</th><th>비율</th></tr>
  {table_html}
</table>
</div>
</body>
</html>"""


@app.get("/stats", response_class=HTMLResponse)
def get_stats(end: Optional[str] = Query(default=None)):
    end = end or now_kst().strftime("%Y-%m-%d")
    try:
        datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return HTMLResponse("<h1>잘못된 날짜 형식 (YYYY-MM-DD)</h1>", status_code=400)
    return HTMLResponse(render_stats(end), headers={"Cache-Control": "no-store"})
