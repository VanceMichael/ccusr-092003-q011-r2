-- 验收平台：参与方、项目、系统版本、基线/环境、里程碑指标、验收轮次、证据摘要
-- 原始业务数据不出境：库里只保存受控引用与 sha256 摘要，不保存任何原始记录。

CREATE TABLE IF NOT EXISTS parties (
    ref         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('institution', 'supplier', 'administrator')),
    contact_ref TEXT,
    token_hash  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_ref     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    domain          TEXT NOT NULL,
    -- 四个阶段必须严格区分：展品 / 试点 / 部署 / 正式验收
    stage           TEXT NOT NULL CHECK (stage IN ('demo', 'pilot', 'deployment', 'acceptance')),
    institution_ref TEXT NOT NULL REFERENCES parties(ref),
    supplier_ref    TEXT NOT NULL REFERENCES parties(ref),
    created_at      TEXT NOT NULL,
    CHECK (institution_ref <> supplier_ref)
);

CREATE TABLE IF NOT EXISTS system_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_ref     TEXT NOT NULL REFERENCES projects(project_ref),
    revision_no     INTEGER NOT NULL,
    version_label   TEXT NOT NULL,
    applicability   TEXT NOT NULL, -- 供应方的适用声明（语言/网络/基础设施前提）
    registered_by   TEXT NOT NULL REFERENCES parties(ref),
    created_at      TEXT NOT NULL,
    UNIQUE (project_ref, revision_no)
);

CREATE TABLE IF NOT EXISTS baselines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_ref   TEXT NOT NULL REFERENCES projects(project_ref),
    requirements  TEXT NOT NULL,  -- JSON: [{code, description}]
    data_summary  TEXT NOT NULL,  -- JSON: 仅允许聚合摘要，禁止原始记录
    digest        TEXT NOT NULL,  -- 服务端计算的 sha256 摘要
    registered_by TEXT NOT NULL REFERENCES parties(ref),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS environments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_ref    TEXT NOT NULL REFERENCES projects(project_ref),
    language       TEXT NOT NULL,
    network        TEXT NOT NULL,
    infrastructure TEXT NOT NULL,
    notes          TEXT,
    digest         TEXT NOT NULL,
    registered_by  TEXT NOT NULL REFERENCES parties(ref),
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_ref TEXT NOT NULL REFERENCES projects(project_ref),
    code        TEXT NOT NULL,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (project_ref, code)
);

CREATE TABLE IF NOT EXISTS milestone_metrics (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id         INTEGER NOT NULL REFERENCES milestones(id),
    metric_code          TEXT NOT NULL,
    name                 TEXT NOT NULL,
    operator             TEXT NOT NULL CHECK (operator IN ('>=', '<=', '>', '<', '=')),
    threshold            REAL NOT NULL,
    proposed_by          TEXT NOT NULL REFERENCES parties(ref),
    institution_agreed_at TEXT,
    supplier_agreed_at   TEXT,
    created_at           TEXT NOT NULL,
    UNIQUE (milestone_id, metric_code)
);

CREATE TABLE IF NOT EXISTS acceptance_rounds (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_ref                 TEXT NOT NULL REFERENCES projects(project_ref),
    milestone_id                INTEGER NOT NULL REFERENCES milestones(id),
    round_no                    INTEGER NOT NULL,
    base_revision_id            INTEGER NOT NULL REFERENCES system_revisions(id),
    -- open（进行中）/ passed / failed（失败，可另开轮次）/ invalidated（版本升级后失效）
    status                      TEXT NOT NULL CHECK (status IN ('open', 'passed', 'failed', 'invalidated')),
    conclusion                  TEXT,
    signature_digest            TEXT,  -- 结论签名：服务端对规范化结论计算 sha256
    opened_by                   TEXT NOT NULL REFERENCES parties(ref),
    submitted_by                TEXT REFERENCES parties(ref),
    created_at                  TEXT NOT NULL,
    concluded_at                TEXT,
    invalidated_at              TEXT,
    invalidated_by_revision_id  INTEGER REFERENCES system_revisions(id),
    UNIQUE (milestone_id, round_no)
);

CREATE TABLE IF NOT EXISTS round_metric_results (
    round_id      INTEGER NOT NULL REFERENCES acceptance_rounds(id),
    metric_id     INTEGER NOT NULL REFERENCES milestone_metrics(id),
    measured_value REAL NOT NULL,
    passed        INTEGER NOT NULL,
    PRIMARY KEY (round_id, metric_id)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id       INTEGER NOT NULL REFERENCES acceptance_rounds(id),
    kind           TEXT NOT NULL,
    label          TEXT NOT NULL,
    digest         TEXT NOT NULL,  -- 仅 sha256(hex)，原始材料留在当地受控存储
    controlled_ref TEXT NOT NULL,  -- 当地受控引用编号
    registered_by  TEXT NOT NULL REFERENCES parties(ref),
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_project ON system_revisions(project_ref);
CREATE INDEX IF NOT EXISTS idx_rounds_project ON acceptance_rounds(project_ref);
CREATE INDEX IF NOT EXISTS idx_evidence_round ON evidence_items(round_id);
