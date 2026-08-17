"""SQLite 온라인 백업. backups/activity-YYYYMMDD.db 생성, 14일 초과분 삭제,
텔레그램으로 백업 파일 전송 (오프사이트 보관 — 디스크 장애 대비).

실행: .venv/bin/python scripts/backup.py (매일 02:00 systemd 타이머)
"""

import json
import sqlite3
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "activity.db"
BACKUP_DIR = ROOT / "backups"
KEEP_DAYS = 14


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


def send_to_telegram(path: Path) -> bool:
    """백업 파일을 텔레그램 봇으로 전송. 실패해도 백업 자체는 성공으로 처리."""
    env = load_env()
    token, chat_id = env.get("TELEGRAM_LOG_BOT"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("텔레그램 설정 없음 — 오프사이트 전송 생략")
        return False
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("chat_id", chat_id),
                        ("caption", f"🗄 activity_log 백업 ({path.stat().st_size // 1024}KB)"),
                        ("disable_notification", "true")):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            .encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        method="POST",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ok = json.loads(r.read()).get("ok", False)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")
        return False
    print("텔레그램 전송 완료" if ok else "텔레그램 전송 실패 (응답 not ok)")
    return ok


def main() -> int:
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"activity-{datetime.now().strftime('%Y%m%d')}.db"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)  # 온라인 백업 API — 쓰기 중에도 일관된 스냅샷
    dst.close()
    src.close()
    print(f"백업 완료: {dest} ({dest.stat().st_size // 1024}KB)")

    send_to_telegram(dest)

    cutoff = time.time() - KEEP_DAYS * 86400
    for f in BACKUP_DIR.glob("activity-*.db"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            print(f"오래된 백업 삭제: {f.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
