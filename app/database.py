
import os
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(database_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    path = Path(database_path or os.getenv("DATABASE_PATH", "data/app.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate(connection: sqlite3.Connection) -> list[str]:
    """按文件名顺序应用全部未执行的迁移，返回本次新应用的版本列表。"""
    connection.executescript(BOOTSTRAP_SQL)
    applied = {
        row[0]
        for row in connection.execute("SELECT version FROM schema_migrations")
    }
    newly_applied: list[str] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = sql_file.stem
        if version in applied:
            continue
        with connection:  # 每个迁移一个事务
            connection.executescript(sql_file.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
        newly_applied.append(version)
    return newly_applied


def main() -> None:
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    connection = connect(database_path)
    try:
        newly_applied = migrate(connection)
    finally:
        connection.close()
    if newly_applied:
        print(f"已应用迁移：{', '.join(newly_applied)}")
    print(f"数据库迁移完成：{database_path}")


if __name__ == "__main__":
    main()
