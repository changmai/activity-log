"""하루 기록 정리 배치: 중복 발화 병합, 회상 입력 시간 배치, 시작/끝 확정.

실행: .venv/bin/python scripts/tidy.py [--date YYYY-MM-DD] [--dry-run] [--rollback RUN_ID]
스케줄: systemd 타이머로 매일 13:00, 23:30 (activity-tidy.timer)

동작:
- prompts/tidy.md(방법론) + data/tidy-rules.md(누적 규칙집)를 claude CLI에 넣어
  {apply, ask, new_rules} JSON 계획을 받는다.
- 규칙에 부합하는 것만 자동 적용(raw_text 불변, 전 변경은 감사 로그에 기록 → 롤백 가능).
- 애매한 것은 텔레그램으로 질문을 보내고, 사용자의 답장은 다음 실행 때 읽어
  해당 건 반영 + 일반화된 규칙으로 규칙집에 축적한다 (질문이 점점 줄어드는 구조).
"""

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.constants import CATEGORIES  # noqa: E402

DB_PATH = ROOT / "data" / "activity.db"
PROMPT_PATH = ROOT / "prompts" / "tidy.md"
RULES_PATH = ROOT / "data" / "tidy-rules.md"
STATE_PATH = ROOT / "data" / "tidy-state.json"
AUDIT_PATH = ROOT / "logs" / "tidy-audit.jsonl"

EDITABLE = ("display_text", "effective_ts", "end_ts", "category", "note")
ALLOWED_NOTES = (None, "merged", "consumed")


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"

logger = logging.getLogger("tidy")
logger.setLevel(logging.INFO)
_h = RotatingFileHandler(ROOT / "logs" / "tidy.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_h)
logger.addHandler(logging.StreamHandler(sys.stdout))


def tg(method: str, payload: dict | None = None) -> dict:
    token = os.environ.get("TELEGRAM_LOG_BOT")
    if not token:
        return {}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        method="POST",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"last_update_id": 0, "pending": []}


def get_replies(last_update_id: int) -> tuple[list[str], int]:
    """마지막으로 처리한 update 이후의 사용자 답장을 수집."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id or not os.environ.get("TELEGRAM_LOG_BOT"):
        return [], last_update_id
    try:
        data = tg("getUpdates", {"offset": last_update_id + 1, "timeout": 0})
    except Exception as e:  # 네트워크 실패는 다음 실행에서 재시도
        logger.warning("getUpdates 실패: %s", e)
        return [], last_update_id
    replies, new_last = [], last_update_id
    for upd in data.get("result", []):
        new_last = max(new_last, upd.get("update_id", 0))
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) == str(chat_id) and msg.get("text"):
            replies.append(msg["text"].strip())
    return replies, new_last


def fetch_days(dates: list[str]) -> list[dict]:
    """대상 날짜(들)의 기록. 어제도 포함해 — 자정 넘김 정리와
    '어제 자동 정리에 대한 지적 답장'을 반영할 수 있게 한다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(dates))
    rows = conn.execute(
        "SELECT id, ts, COALESCE(effective_ts, ts) AS start, effective_ts, end_ts, "
        "raw_text, display_text, category, source, hidden, note FROM events "
        f"WHERE substr(COALESCE(effective_ts, ts), 1, 10) IN ({marks}) "
        "ORDER BY COALESCE(effective_ts, ts), id",
        dates,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def parse_ts(value):
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"응답에서 JSON을 찾지 못함: {text[:200]}")
    return json.loads(m.group())


def validate_item(item: dict, rows_by_id: dict, ongoing_id) -> str | None:
    """apply 항목 검증. 문제 있으면 사유 문자열, 통과면 None."""
    eid = item.get("id")
    if eid not in rows_by_id:
        return f"id {eid}: 대상 날짜의 이벤트가 아님"
    row = rows_by_id[eid]
    unknown = [k for k in item if k not in EDITABLE and k != "id"]
    if unknown:
        return f"id {eid}: 허용되지 않는 필드 {unknown}"
    if "note" in item and item["note"] not in ALLOWED_NOTES:
        return f"id {eid}: 허용되지 않는 note '{item['note']}'"
    if "category" in item and item["category"] is not None and item["category"] not in CATEGORIES:
        return f"id {eid}: 알 수 없는 카테고리 '{item['category']}'"
    if eid == ongoing_id and ("end_ts" in item or item.get("note") == "merged"):
        return f"id {eid}: 진행 중인 마지막 기록 — 종료/병합 금지"
    base = parse_ts(row["ts"])
    eff = item.get("effective_ts", "keep")
    end = item.get("end_ts", "keep")
    new_start = parse_ts(eff) if eff not in ("keep", None) else parse_ts(row["start"])
    if eff not in ("keep", None):
        dt = parse_ts(eff)
        if dt is None or base is None or abs((dt - base).total_seconds()) > 48 * 3600:
            return f"id {eid}: effective_ts 비정상 ({eff})"
    if end not in ("keep", None):
        dt = parse_ts(end)
        if dt is None or new_start is None:
            return f"id {eid}: end_ts 파싱 실패 ({end})"
        if dt <= new_start or dt - new_start > timedelta(hours=18):
            return f"id {eid}: end_ts 범위 오류 (시작 {new_start} → 끝 {dt})"
    return None


def _fmt_change(row: dict, fields: dict) -> str:
    """적용 내역을 사용자에게 보여줄 한 줄로."""
    name = (row["display_text"] or row["raw_text"])[:24]
    parts = []
    if "display_text" in fields:
        parts.append(f"표시 '{fields['display_text']}'" if fields["display_text"] else "표시 복원")
    if "effective_ts" in fields:
        parts.append(f"시작 {fields['effective_ts'][11:16]}" if fields["effective_ts"] else "시작 해제")
    if "end_ts" in fields:
        parts.append(f"종료 {fields['end_ts'][11:16]}" if fields["end_ts"] else "종료 해제")
    if "category" in fields:
        parts.append(f"분류 {fields['category']}")
    if fields.get("note") == "merged":
        parts.append("중복 병합")
    if fields.get("note") == "consumed":
        parts.append("종료 보고로 처리")
    return f"{name} → " + ", ".join(parts)


def apply_plan(items: list[dict], rows_by_id: dict, run_id: str, dry: bool
               ) -> tuple[int, list[str], list[str]]:
    conn = sqlite3.connect(DB_PATH)
    applied, rejected, summaries = 0, [], []
    # '진행 중' = 종료가 없고, DB 전체에서 그보다 뒤에 아무 기록도 없으며, 시작 24h 이내
    starts = {i: parse_ts(r["start"]) for i, r in rows_by_id.items()}
    open_rows = [i for i, r in rows_by_id.items() if not r["end_ts"] and not (r["note"] or "")]
    ongoing_id = None
    if open_rows:
        cand = max(open_rows, key=lambda i: starts[i])
        has_later = conn.execute(
            "SELECT 1 FROM events WHERE COALESCE(effective_ts, ts) > ? LIMIT 1",
            (rows_by_id[cand]["start"],),
        ).fetchone()
        if not has_later and datetime.now(KST) - starts[cand] < timedelta(hours=24):
            ongoing_id = cand
    with AUDIT_PATH.open("a", encoding="utf-8") as audit:
        for item in items:
            why = validate_item(item, rows_by_id, ongoing_id)
            if why:
                rejected.append(why)
                continue
            eid = item["id"]
            fields = {k: item[k] for k in EDITABLE if k in item}
            before = {k: rows_by_id[eid][k] for k in fields}
            if all(before[k] == v for k, v in fields.items()):
                continue  # 변경 없음
            if not dry:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE events SET {sets} WHERE id = ?", [*fields.values(), eid])
                audit.write(json.dumps(
                    {"run": run_id, "at": datetime.now(KST).isoformat(timespec="seconds"),
                     "id": eid, "before": before, "after": fields}, ensure_ascii=False) + "\n")
            applied += 1
            summaries.append(_fmt_change(rows_by_id[eid], fields))
            logger.info("%s id=%s %s -> %s", "적용" if not dry else "(dry)", eid, before, fields)
    conn.commit()
    conn.close()
    return applied, rejected, summaries


def apply_creates(items: list[dict], rows_by_id: dict, run_id: str, now: datetime, dry: bool
                  ) -> tuple[int, list[str], list[str]]:
    """구간 분할 생성: 여러 구간을 담은 회상 발화를 새 블록으로 분할.
    생성 블록은 시작/종료 필수, note='split-from-{원본id}', 감사 로그로 롤백 가능."""
    conn = sqlite3.connect(DB_PATH)
    created, rejected, summaries = 0, [], []
    with AUDIT_PATH.open("a", encoding="utf-8") as audit:
        for item in items[:12]:  # 폭주 방지 상한
            text = (item.get("text") or "").strip()
            cat = item.get("category")
            src = item.get("source_id")
            sd, ed = parse_ts(item.get("effective_ts")), parse_ts(item.get("end_ts"))
            if not text or len(text) > 100:
                rejected.append(f"create: 텍스트 비정상 ({text[:30]!r})")
                continue
            if cat not in CATEGORIES:
                rejected.append(f"create: 알 수 없는 카테고리 '{cat}'")
                continue
            if src not in rows_by_id:
                rejected.append(f"create: source_id {src}가 대상 기록에 없음")
                continue
            if sd is None or ed is None or ed <= sd or ed - sd > timedelta(hours=18):
                rejected.append(f"create: 시각 범위 오류 ({item.get('effective_ts')} ~ {item.get('end_ts')})")
                continue
            if not (now - timedelta(hours=72) < sd <= now):
                rejected.append(f"create: 시작이 허용 범위(최근 72h) 밖 ({sd})")
                continue
            note = f"split-from-{src}"
            s_iso, e_iso = sd.isoformat(timespec="seconds"), ed.isoformat(timespec="seconds")
            dup = conn.execute("SELECT 1 FROM events WHERE note = ? AND effective_ts = ?",
                               (note, s_iso)).fetchone()
            if dup:
                continue  # 재실행 멱등성
            if not dry:
                cur = conn.execute(
                    "INSERT INTO events (ts, received_at, raw_text, source, category, "
                    "effective_ts, end_ts, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (s_iso, now.isoformat(timespec="seconds"),
                     f"{text} ({src} 회상 발화에서 분리)", "tidy", cat, s_iso, e_iso, note))
                audit.write(json.dumps(
                    {"run": run_id, "at": datetime.now(KST).isoformat(timespec="seconds"),
                     "created": cur.lastrowid,
                     "fields": {"text": text, "category": cat, "effective_ts": s_iso,
                                "end_ts": e_iso, "note": note}}, ensure_ascii=False) + "\n")
            created += 1
            summaries.append(f"신규 {sd.strftime('%H:%M')}~{ed.strftime('%H:%M')} {cat} {text}"
                             f" (id {src}에서 분리)")
            logger.info("%s 생성 %s~%s %s %s (src=%s)", "(dry)" if dry else "",
                        s_iso, e_iso, cat, text, src)
    conn.commit()
    conn.close()
    return created, rejected, summaries


def rollback(run_id: str) -> int:
    if not AUDIT_PATH.exists():
        print("감사 로그 없음")
        return 1
    conn = sqlite3.connect(DB_PATH)
    n = 0
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec["run"] != run_id:
            continue
        if "created" in rec:  # 분할로 생성된 블록은 삭제로 롤백
            conn.execute("DELETE FROM events WHERE id = ?", (rec["created"],))
        else:
            sets = ", ".join(f"{k} = ?" for k in rec["before"])
            conn.execute(f"UPDATE events SET {sets} WHERE id = ?",
                         [*rec["before"].values(), rec["id"]])
        n += 1
    conn.commit()
    conn.close()
    print(f"롤백 완료: run={run_id}, {n}건 복원")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", metavar="RUN_ID")
    args = ap.parse_args()
    if args.rollback:
        return rollback(args.rollback)

    now = datetime.now(KST)
    date = args.date or now.strftime("%Y-%m-%d")
    prev_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    run_id = now.strftime("%Y%m%d-%H%M%S")
    rows = fetch_days([prev_date, date])
    if not rows:
        logger.info("%s 기록 없음 — 종료", date)
        return 0
    rows_by_id = {r["id"]: r for r in rows}

    state = load_state()
    replies, new_last = get_replies(state.get("last_update_id", 0))
    pending = state.get("pending", [])

    events_json = json.dumps(
        [{k: r[k] for k in ("id", "start", "end_ts", "raw_text", "display_text",
                            "category", "source", "hidden", "note")} for r in rows],
        ensure_ascii=False, indent=1)
    sections = [
        PROMPT_PATH.read_text(encoding="utf-8"),
        "\n## 규칙집\n" + (RULES_PATH.read_text(encoding="utf-8") if RULES_PATH.exists() else "(비어 있음)"),
        f"\n## 현재 시각\n{now.isoformat(timespec='seconds')} (대상 날짜: {date}, 전날 {prev_date} 기록도 수정 가능)",
        "\n## 기록 (전날 + 대상 날짜)\n" + events_json,
    ]
    if state.get("last_applied"):
        sections.append("\n## 직전 실행에서 자동 적용된 정리 내역 (사용자 답장이 이를 지적할 수 있음)\n"
                        + "\n".join(f"{i+1}. {s}" for i, s in enumerate(state["last_applied"])))
    if pending:
        sections.append("\n## 이전 실행에서 보낸 질문 (미해결)\n"
                        + "\n".join(f"{i+1}. {q['question']} (관련 id: {q['ids']})"
                                    for i, q in enumerate(pending)))
    if replies:
        sections.append("\n## 사용자 답변 (위 질문에 대한 답일 수 있음)\n"
                        + "\n".join(f"- {r}" for r in replies))
    prompt = "\n".join(sections)

    try:
        proc = subprocess.run([CLAUDE_BIN, "-p", "--output-format", "json"],
                              input=prompt, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        logger.error("claude CLI 타임아웃 — 다음 실행 때 재시도")
        return 1
    if proc.returncode != 0:
        logger.error("claude CLI 실패 (exit=%d): %s", proc.returncode, proc.stderr[:300])
        return 1
    try:
        wrapper = json.loads(proc.stdout)
        plan = extract_json(wrapper["result"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error("응답 파싱 실패: %s", e)
        return 1

    applied, rejected, summaries = apply_plan(plan.get("apply") or [], rows_by_id, run_id, args.dry_run)
    created, c_rejected, c_summaries = apply_creates(
        plan.get("create") or [], rows_by_id, run_id, now, args.dry_run)
    rejected += c_rejected
    summaries += c_summaries
    for why in rejected:
        logger.warning("거부: %s", why)

    new_rules = [r for r in (plan.get("new_rules") or []) if isinstance(r, str) and r.strip()]
    if new_rules and replies and not args.dry_run:
        with RULES_PATH.open("a", encoding="utf-8") as f:
            for r in new_rules:
                f.write(f"- ({date}) {r.strip()}\n")
        logger.info("규칙 %d개 학습: %s", len(new_rules), new_rules)

    asks = [a for a in (plan.get("ask") or []) if isinstance(a, dict) and a.get("question")]
    # 정리 보고: 자동 적용 내역은 항상 알려서 사용자가 검증할 수 있게 한다.
    # 질문이 없으면 무음 알림 (열어보고 잘못된 게 있으면 답장으로 지적 → 다음 실행이 교정·학습)
    if (summaries or asks) and not args.dry_run:
        parts = [f"🧹 기록 정리 ({date} {now.strftime('%H:%M')})"]
        if summaries:
            parts.append("자동 정리:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(summaries)))
        if asks:
            parts.append("질문:\n" + "\n".join(f"{i+1}. {a['question']}" for i, a in enumerate(asks)))
        parts.append("잘못된 정리 지적이나 질문 답변은 답장으로 — 다음 정리 때 반영·학습됩니다.")
        try:
            tg("sendMessage", {"chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
                               "text": "\n\n".join(parts), "disable_web_page_preview": True,
                               "disable_notification": not asks})
        except Exception as e:
            logger.warning("정리 보고 전송 실패: %s", e)

    if not args.dry_run:
        STATE_PATH.write_text(json.dumps(
            {"last_update_id": new_last,
             "pending": [{"question": a["question"], "ids": a.get("ids", [])} for a in asks],
             "last_applied": summaries},
            ensure_ascii=False, indent=1), encoding="utf-8")

    logger.info("run=%s 적용 %d건, 생성 %d건, 거부 %d건, 질문 %d건, 새 규칙 %d개 (cost=$%.4f)%s",
                run_id, applied, created, len(rejected), len(asks), len(new_rules),
                wrapper.get("total_cost_usd") or 0, " [dry-run]" if args.dry_run else "")
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
