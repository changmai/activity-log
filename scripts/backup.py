"""SQLite 온라인 백업. backups/activity-YYYYMMDD.db 생성, 14일 초과분 삭제.

실행: .venv/bin/python scripts/backup.py (매일 02:00 systemd 타이머)
"""

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "activity.db"
BACKUP_DIR = ROOT / "backups"
KEEP_DAYS = 14


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

    cutoff = time.time() - KEEP_DAYS * 86400
    for f in BACKUP_DIR.glob("activity-*.db"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            print(f"오래된 백업 삭제: {f.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
