"""执行 migrations 目录下的全部 SQL 迁移（按文件名排序）。"""

import os
import sqlite3
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def migrate(database_path: Path | None = None) -> Path:
    database_path = database_path or Path(
        os.getenv("DATABASE_PATH", "data/app.sqlite3")
    )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY,"
            "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in applied:
                continue
            connection.executescript(path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (path.stem,),
            )
            print(f"已应用迁移：{path.stem}")
    return database_path


if __name__ == "__main__":
    print(f"数据库迁移完成：{migrate()}")
