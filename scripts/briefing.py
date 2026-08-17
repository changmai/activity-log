"""전날 활동 브리핑을 텔레그램으로 전송. 매일 07:00 systemd 타이머.

실행: .venv/bin/python scripts/briefing.py [YYYY-MM-DD]  (날짜 생략 시 어제)
- .env의 TELEGRAM_LOG_BOT (봇 토큰) 필요
- TELEGRAM_CHAT_ID가 없으면 getUpdates로 자동 발견해 .env에 저장
  (사용자가 봇에게 /start 를 한 번 보내두어야 함)
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.constants import UNCATEGORIZED  # noqa: E402
from app.main import _visible_minutes, build_blocks, now_kst, union_minutes  # noqa: E402

logger = logging.getLogger("briefing")
logger.setLevel(logging.INFO)
_h = RotatingFileHandler(ROOT / "logs" / "briefing.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_h)
logger.addHandler(logging.StreamHandler(sys.stdout))


def load_env() -> dict:
    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


# 링크용 서버 주소는 .env의 TAILSCALE_URL에서 — IP를 저장소에 커밋하지 않기 위함
TAILSCALE_URL = load_env().get("TAILSCALE_URL", "http://localhost:8787")


def tg(token: str, method: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        method="POST",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def discover_chat_id(token: str) -> str | None:
    """getUpdates에서 가장 최근 대화의 chat_id를 찾아 .env에 저장."""
    data = tg(token, "getUpdates")
    chat_id = None
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        cid = (msg.get("chat") or {}).get("id")
        if cid:
            chat_id = str(cid)
    if chat_id:
        env_path = ROOT / ".env"
        content = env_path.read_text(encoding="utf-8")
        # 기존 파일이 개행 없이 끝나면 붙어버리므로 개행 보장
        if content and not content.endswith("\n"):
            content += "\n"
        env_path.write_text(content + f"TELEGRAM_CHAT_ID={chat_id}\n", encoding="utf-8")
        logger.info("chat_id 발견 및 저장: %s", chat_id)
    return chat_id


def fmt_min(m: int) -> str:
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def compose(date: str) -> str:
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    from datetime import datetime as _dt
    d = _dt.strptime(date, "%Y-%m-%d")
    head = f"📅 {d.month}월 {d.day}일({weekdays[d.weekday()]}) 활동 브리핑"

    blocks = build_blocks(date)
    totals: dict[str, int] = {}
    for b in blocks:
        if b["empty"] or b["hidden"]:
            continue
        key = b["category"] or UNCATEGORIZED
        totals[key] = totals.get(key, 0) + _visible_minutes(b)

    if not totals:
        return f"{head}\n\n기록이 없어요."

    recorded = sum(totals.values())
    covered = union_minutes(blocks)  # 겹침(병렬 활동)은 한 번만 — 커버리지 지표
    cov_pct = round(covered * 100 / (24 * 60))
    lines = [
        head,
        f"총 기록 {fmt_min(recorded)}",
        f"📊 커버리지 {cov_pct}% — 빈 시간 {fmt_min(24 * 60 - covered)}",
        "",
    ]
    for cat, m in sorted(totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"· {cat} {fmt_min(m)}")
    kill = totals.get("킬링타임", 0)
    if kill:
        lines.append("")
        lines.append(f"⏳ 킬링타임 {fmt_min(kill)} — 기록의 {round(kill * 100 / recorded)}%")
    lines.append("")
    lines.append(f"타임라인: {TAILSCALE_URL}/timeline?date={date}")
    lines.append(f"주간 통계: {TAILSCALE_URL}/stats?end={date}")
    return "\n".join(lines)


def main() -> int:
    env = load_env()
    token = env.get("TELEGRAM_LOG_BOT")
    if not token:
        logger.error("TELEGRAM_LOG_BOT 이 .env에 없음 — 브리핑 생략")
        return 1

    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        try:
            chat_id = discover_chat_id(token)
        except urllib.error.URLError as e:
            logger.error("텔레그램 접속 실패: %s", e)
            return 1
        if not chat_id:
            logger.error("chat_id를 찾지 못함 — 봇에게 /start 를 먼저 보내주세요")
            return 1

    date = sys.argv[1] if len(sys.argv) > 1 else (now_kst() - timedelta(days=1)).strftime("%Y-%m-%d")
    text = compose(date)
    try:
        res = tg(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                        "disable_web_page_preview": True})
    except urllib.error.HTTPError as e:
        logger.error("전송 실패 %s: %s", e.code, e.read().decode()[:200])
        return 1
    except urllib.error.URLError as e:
        logger.error("텔레그램 접속 실패: %s", e)
        return 1
    if not res.get("ok"):
        logger.error("전송 실패: %s", res)
        return 1
    logger.info("브리핑 전송 완료 (%s)", date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
