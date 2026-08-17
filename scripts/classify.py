"""미분류 이벤트를 Claude Code CLI(헤드리스)로 분류해 category/tags/effective_ts를 채우는 배치.

실행: .venv/bin/python scripts/classify.py  (표준 라이브러리만 사용)
- category IS NULL 인 이벤트만 처리 (멱등)
- raw_text는 절대 수정하지 않는다
- "끝"/"종료"/"end" 마커는 CLI 호출 없이 '기타'로 처리
- 실패 시 미분류로 남겨 다음 실행 때 재시도
"""

import json
import logging
import re
import sqlite3
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.constants import CATEGORIES  # noqa: E402  (서버와 동일 목록 공유)

DB_PATH = ROOT / "data" / "activity.db"
PROMPT_PATH = ROOT / "prompts" / "classify.md"
CLAUDE_BIN = "CLAUDE_BIN_PATH"

logger = logging.getLogger("classify")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(ROOT / "logs" / "classify.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)
logger.addHandler(logging.StreamHandler(sys.stdout))


def extract_json(text: str) -> dict:
    # 코드펜스나 앞뒤 설명이 섞여도 첫 JSON 오브젝트를 추출
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"응답에서 JSON을 찾지 못함: {text[:200]}")
    return json.loads(match.group())


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 마커 이벤트는 CLI 없이 처리
    cur = conn.execute(
        "UPDATE events SET category = '기타', tags = '[]' "
        "WHERE category IS NULL AND lower(trim(raw_text)) IN ('끝', '종료', 'end')"
    )
    if cur.rowcount:
        logger.info("마커 이벤트 %d건을 '기타'로 처리", cur.rowcount)
    conn.commit()

    rows = conn.execute(
        "SELECT id, ts, raw_text, display_text FROM events WHERE category IS NULL ORDER BY ts ASC"
    ).fetchall()
    if not rows:
        logger.info("미분류 이벤트 없음")
        return 0

    # 사용자가 교정한 텍스트(display_text)가 있으면 그것으로 분류 (STT 오인식 교정 반영)
    events_json = json.dumps(
        [{"id": r["id"], "ts": r["ts"], "text": r["display_text"] or r["raw_text"]} for r in rows],
        ensure_ascii=False,
    )
    prompt = (
        PROMPT_PATH.read_text(encoding="utf-8")
        + "\n\n## 출력 형식\n"
        + '반드시 아래 형태의 JSON만 출력한다. 설명 문장, 코드펜스, 도구 사용 없이 JSON만:\n'
        + '{"results": [{"id": 1, "category": "업무", "tags": ["회의"], "effective_ts": null}, ...]}\n'
        + "\n## 분류할 이벤트\n"
        + events_json
    )

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        logger.error("claude CLI 타임아웃 — 미분류 유지, 다음 실행 때 재시도")
        return 1
    if proc.returncode != 0:
        logger.error("claude CLI 실패 (exit=%d): %s — 미분류 유지", proc.returncode, proc.stderr[:300])
        return 1

    try:
        wrapper = json.loads(proc.stdout)
        results = extract_json(wrapper["result"])["results"]
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error("응답 파싱 실패: %s — 미분류 유지", e)
        return 1

    valid_ids = {r["id"] for r in rows}
    updated, skipped = 0, 0
    for item in results:
        if item.get("id") not in valid_ids or item.get("category") not in CATEGORIES:
            skipped += 1
            continue
        # 재분류 시 기존 effective_ts는 보존 (모델이 새 값을 주지 않으면 유지)
        conn.execute(
            "UPDATE events SET category = ?, tags = ?, "
            "effective_ts = COALESCE(?, effective_ts) "
            "WHERE id = ? AND category IS NULL",
            (
                item["category"],
                json.dumps(item.get("tags") or [], ensure_ascii=False),
                item.get("effective_ts"),
                item["id"],
            ),
        )
        updated += 1
    conn.commit()
    if skipped:
        logger.warning("잘못된 항목 %d건 무시", skipped)
    logger.info("%d건 분류 완료 (claude code, cost=$%.4f)", updated, wrapper.get("total_cost_usd") or 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
