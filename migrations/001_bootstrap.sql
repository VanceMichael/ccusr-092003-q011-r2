
-- 基础迁移：迁移版本表由 app/database.py 的运行器统一创建并记录版本，
-- 这里保留建表语句以便直接执行 SQL 时也可重复运行（IF NOT EXISTS）。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
