-- 验收平台领域模型：只保存需求基线、环境说明、数据摘要、系统版本、
-- 指标约定、签名结论与证据摘要，不保存任何原始业务数据。

CREATE TABLE parties (
    party_id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('local_institution', 'supplier')),
    label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES parties(party_id),
    created_at TEXT NOT NULL
);

-- 项目参与方名册：证据摘要及全部项目明细只对名册内参与方可见
CREATE TABLE project_participants (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    role TEXT NOT NULL CHECK (role IN ('local_institution', 'supplier')),
    joined_at TEXT NOT NULL,
    PRIMARY KEY (project_id, party_id)
);

CREATE TABLE requirement_baselines (
    baseline_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    seq INTEGER NOT NULL,
    requirements TEXT NOT NULL,            -- JSON：需求基线内容
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, seq)
);

CREATE TABLE environment_notes (
    note_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    languages TEXT NOT NULL,               -- JSON 数组：当地语言
    network TEXT NOT NULL,                 -- JSON：网络条件摘要
    infrastructure TEXT NOT NULL,          -- JSON：基础设施条件摘要
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE data_summaries (
    summary_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    dataset_ref TEXT NOT NULL,             -- 受控引用编号，不含真实身份
    digest_algorithm TEXT NOT NULL DEFAULT 'sha256',
    digest TEXT NOT NULL,                  -- 仅保存 sha256 摘要
    row_count INTEGER,
    schema_note TEXT NOT NULL,             -- JSON：抽象字段说明，不含原始记录
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE system_versions (
    version_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    revision INTEGER NOT NULL,
    label TEXT NOT NULL,
    release_note TEXT NOT NULL DEFAULT '',
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, revision)
);

CREATE TABLE applicability_claims (
    claim_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES system_versions(version_id),
    scope TEXT NOT NULL,                   -- 适用范围
    languages TEXT NOT NULL,               -- JSON：声明支持的语言
    network_conditions TEXT NOT NULL,      -- JSON：声明适用的网络条件
    infrastructure_conditions TEXT NOT NULL, -- JSON：声明适用的基础设施条件
    limitations TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE milestones (
    milestone_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    milestone_ref TEXT NOT NULL,
    title TEXT NOT NULL,
    -- 组合视图四阶段：展品 / 试点 / 部署 / 正式验收，严格分开
    stage TEXT NOT NULL CHECK (stage IN (
        'demonstration', 'pilot', 'deployment', 'formal_acceptance'
    )),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, milestone_ref)
);

CREATE TABLE milestone_metrics (
    metric_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    metric_key TEXT NOT NULL,
    name TEXT NOT NULL,
    comparison TEXT NOT NULL CHECK (comparison IN ('gte', 'lte', 'eq')),
    threshold REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (milestone_id, metric_key)
);

-- 指标须由当地机构与供应方双方分别确认
CREATE TABLE metric_consents (
    metric_id TEXT NOT NULL REFERENCES milestone_metrics(metric_id),
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    role TEXT NOT NULL CHECK (role IN ('local_institution', 'supplier')),
    consented_at TEXT NOT NULL,
    PRIMARY KEY (metric_id, party_id)
);

CREATE TABLE acceptance_rounds (
    round_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    round_no INTEGER NOT NULL,             -- 失败重测另开轮次，序号递增
    revision INTEGER NOT NULL,             -- 本轮所针对的系统版本号
    status TEXT NOT NULL CHECK (status IN ('open', 'passed', 'failed', 'superseded')),
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE (milestone_id, round_no)
);

CREATE TABLE conclusions (
    conclusion_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL UNIQUE REFERENCES acceptance_rounds(round_id),
    decision TEXT NOT NULL CHECK (decision IN ('pass', 'fail')),
    measurements TEXT NOT NULL,            -- JSON：{metric_key: 实测值}
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 签名结论：通过结论须由当地机构与供应方双方签名后方可封轮
CREATE TABLE conclusion_signatures (
    conclusion_id TEXT NOT NULL REFERENCES conclusions(conclusion_id),
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    role TEXT NOT NULL,
    signature_algorithm TEXT NOT NULL,
    signature_value TEXT NOT NULL,
    signed_payload_digest TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (conclusion_id, party_id)
);

CREATE TABLE evidence_summaries (
    evidence_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    round_id TEXT REFERENCES acceptance_rounds(round_id),
    title TEXT NOT NULL,
    digest_algorithm TEXT NOT NULL DEFAULT 'sha256',
    digest TEXT NOT NULL,                  -- 只有摘要，没有原始材料
    storage_ref TEXT NOT NULL,             -- 受控存储引用
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);

CREATE INDEX idx_rounds_project ON acceptance_rounds(project_id);
CREATE INDEX idx_evidence_project ON evidence_summaries(project_id);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_acceptance');
