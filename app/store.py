"""领域服务层：所有业务规则与状态变更都在这里，HTTP 层只做报文适配。

关键不变量：
1. 平台不接收原始业务数据，只存需求基线、环境说明、数据 *摘要*、
   sha256 摘要与签名结果；
2. 里程碑指标必须经当地机构与供应方双方确认后，才能开启验收轮次；
3. 失败重测另开轮次（round_no 递增），不覆盖历史；
4. 系统版本升级会使该项目所有尚未关闭的轮次失效（superseded）；
5. 证据摘要只对项目名册内参与方可见；
6. 通过结论须由双方签名封轮。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
STAGES = ("demonstration", "pilot", "deployment", "formal_acceptance")

_SCHEMA = Path(__file__).resolve().parent.parent / "migrations"


class DomainError(Exception):
    """领域规则冲突，映射为 4xx。"""

    status = 400

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        if status is not None:
            self.status = status


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _check_ref(value: str, field: str) -> None:
    if not isinstance(value, str) or not REF_PATTERN.match(value):
        raise DomainError(
            "invalid_ref",
            f"{field} 必须是 1-64 位字母数字及 ._-，且以字母数字开头",
        )


def _require(payload: dict[str, Any], field: str, types: tuple[type, ...] | type = str):
    value = payload.get(field)
    if not isinstance(value, types) or (isinstance(value, str) and not value.strip()):
        raise DomainError("invalid_field", f"字段 {field} 缺失或类型不正确")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---- 参与方与项目 -------------------------------------------------

    def register_party(
        self, party_id: str, role: str, label: str, token: str
    ) -> dict[str, Any]:
        _check_ref(party_id, "party_id")
        if role not in ("local_institution", "supplier"):
            raise DomainError("invalid_role", "role 必须是 local_institution/supplier")
        if not isinstance(label, str) or not label.strip():
            raise DomainError("invalid_field", "label 缺失")
        if not isinstance(token, str) or len(token) < 16:
            raise DomainError("invalid_field", "token 至少 16 个字符")
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO parties(party_id, role, label, token_hash, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (party_id, role, label, _digest(token), now_iso()),
                )
        except sqlite3.IntegrityError:
            raise DomainError(
                "conflict", "参与方编号或令牌已存在", 409
            ) from None
        return {"party_id": party_id, "role": role, "label": label}

    def authenticate(self, token: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT party_id, role, label FROM parties WHERE token_hash=?",
                (_digest(token),),
            ).fetchone()
        return dict(row) if row else None

    def create_project(
        self, project_id: str, title: str, party: dict[str, Any]
    ) -> dict[str, Any]:
        _check_ref(project_id, "project_id")
        if not isinstance(title, str) or not title.strip():
            raise DomainError("invalid_field", "title 缺失")
        ts = now_iso()
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO projects(project_id, title, created_by, created_at)"
                    " VALUES (?,?,?,?)",
                    (project_id, title, party["party_id"], ts),
                )
                conn.execute(
                    "INSERT INTO project_participants(project_id, party_id, role, joined_at)"
                    " VALUES (?,?,?,?)",
                    (project_id, party["party_id"], party["role"], ts),
                )
        except sqlite3.IntegrityError:
            raise DomainError("conflict", "项目编号已存在", 409) from None
        return {"project_id": project_id, "title": title}

    def add_participant(self, project_id: str, party_id: str) -> None:
        _check_ref(party_id, "party_id")
        with self.connect() as conn:
            project = conn.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise DomainError("not_found", "项目不存在", 404)
            party = conn.execute(
                "SELECT role FROM parties WHERE party_id=?", (party_id,)
            ).fetchone()
            if party is None:
                raise DomainError("not_found", "参与方不存在", 404)
            try:
                conn.execute(
                    "INSERT INTO project_participants(project_id, party_id, role, joined_at)"
                    " VALUES (?,?,?,?)",
                    (project_id, party_id, party["role"], now_iso()),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "该参与方已在项目名册中", 409) from None

    def _membership(
        self, conn: sqlite3.Connection, project_id: str, party_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT role FROM project_participants"
            " WHERE project_id=? AND party_id=?",
            (project_id, party_id),
        ).fetchone()

    def _require_member(
        self, conn: sqlite3.Connection, project_id: str, party: dict[str, Any]
    ) -> sqlite3.Row:
        row = self._membership(conn, project_id, party["party_id"])
        if row is None:
            raise DomainError("forbidden", "不是该项目的参与方", 403)
        return row

    def _require_role(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        party: dict[str, Any],
        role: str,
    ) -> None:
        row = self._require_member(conn, project_id, party)
        if row["role"] != role:
            raise DomainError(
                "forbidden", f"该操作仅允许 {role} 角色执行", 403
            )

    def list_projects(self, party: dict[str, Any]) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT p.project_id, p.title, p.created_at FROM projects p"
                " JOIN project_participants pp ON pp.project_id=p.project_id"
                " WHERE pp.party_id=? ORDER BY p.created_at",
                (party["party_id"],),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- 当地机构登记：需求基线 / 环境说明 / 数据摘要 ------------------

    def register_baseline(
        self, project_id: str, payload: dict[str, Any], party: dict[str, Any]
    ) -> dict[str, Any]:
        baseline_id = _require(payload, "baseline_id")
        _check_ref(baseline_id, "baseline_id")
        requirements = payload.get("requirements")
        if not isinstance(requirements, dict) or not requirements:
            raise DomainError("invalid_field", "requirements 必须是非空对象")
        with self.connect() as conn:
            self._require_role(conn, project_id, party, "local_institution")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM requirement_baselines"
                " WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            try:
                conn.execute(
                    "INSERT INTO requirement_baselines"
                    "(baseline_id, project_id, seq, requirements, registered_by, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        baseline_id,
                        project_id,
                        seq,
                        _json_dumps(requirements),
                        party["party_id"],
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "基线编号已存在", 409) from None
        return {"baseline_id": baseline_id, "seq": seq}

    def register_environment(
        self, project_id: str, payload: dict[str, Any], party: dict[str, Any]
    ) -> dict[str, Any]:
        note_id = _require(payload, "note_id")
        _check_ref(note_id, "note_id")
        languages = _require(payload, "languages", list)
        network = _require(payload, "network", dict)
        infrastructure = _require(payload, "infrastructure", dict)
        if not languages or not all(isinstance(x, str) and x for x in languages):
            raise DomainError("invalid_field", "languages 必须是非空字符串数组")
        with self.connect() as conn:
            self._require_role(conn, project_id, party, "local_institution")
            try:
                conn.execute(
                    "INSERT INTO environment_notes"
                    "(note_id, project_id, languages, network, infrastructure,"
                    " registered_by, created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        note_id,
                        project_id,
                        _json_dumps(languages),
                        _json_dumps(network),
                        _json_dumps(infrastructure),
                        party["party_id"],
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "环境说明编号已存在", 409) from None
        return {"note_id": note_id}

    def register_data_summary(
        self, project_id: str, payload: dict[str, Any], party: dict[str, Any]
    ) -> dict[str, Any]:
        summary_id = _require(payload, "summary_id")
        _check_ref(summary_id, "summary_id")
        dataset_ref = _require(payload, "dataset_ref")
        digest = _require(payload, "digest")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DomainError("invalid_field", "digest 必须是 64 位十六进制 sha256")
        schema_note = _require(payload, "schema_note", dict)
        row_count = payload.get("row_count")
        if row_count is not None and (
            not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0
        ):
            raise DomainError("invalid_field", "row_count 必须是非负整数")
        with self.connect() as conn:
            self._require_role(conn, project_id, party, "local_institution")
            try:
                conn.execute(
                    "INSERT INTO data_summaries"
                    "(summary_id, project_id, dataset_ref, digest_algorithm, digest,"
                    " row_count, schema_note, registered_by, created_at)"
                    " VALUES (?,?, 'sha256', ?,?,?,?,?,?)",
                    (
                        summary_id,
                        project_id,
                        dataset_ref,
                        digest,
                        row_count,
                        _json_dumps(schema_note),
                        party["party_id"],
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "数据摘要编号已存在", 409) from None
        return {"summary_id": summary_id, "digest": digest}

    # ---- 供应方登记：系统版本与适用声明 -------------------------------

    def register_version(
        self, project_id: str, payload: dict[str, Any], party: dict[str, Any]
    ) -> dict[str, Any]:
        version_id = _require(payload, "version_id")
        _check_ref(version_id, "version_id")
        revision = _require(payload, "revision", int)
        if isinstance(revision, bool) or revision < 1:
            raise DomainError("invalid_field", "revision 必须是不小于 1 的整数")
        label = _require(payload, "label")
        release_note = payload.get("release_note", "")
        if not isinstance(release_note, str):
            raise DomainError("invalid_field", "release_note 必须是字符串")
        ts = now_iso()
        with self.connect() as conn:
            self._require_role(conn, project_id, party, "supplier")
            latest = conn.execute(
                "SELECT COALESCE(MAX(revision),0) FROM system_versions"
                " WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            if revision <= latest:
                raise DomainError(
                    "revision_too_old",
                    f"新版本号必须大于当前最新版本 {latest}",
                    409,
                )
            try:
                conn.execute(
                    "INSERT INTO system_versions"
                    "(version_id, project_id, revision, label, release_note,"
                    " registered_by, created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        version_id,
                        project_id,
                        revision,
                        label,
                        release_note,
                        party["party_id"],
                        ts,
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "版本编号已存在", 409) from None
            # 版本升级：尚未关闭的验收轮次立即失效
            conn.execute(
                "UPDATE acceptance_rounds SET status='superseded', closed_at=?"
                " WHERE project_id=? AND status='open'",
                (ts, project_id),
            )
        return {"version_id": version_id, "revision": revision}

    def register_claim(
        self, project_id: str, payload: dict[str, Any], party: dict[str, Any]
    ) -> dict[str, Any]:
        claim_id = _require(payload, "claim_id")
        _check_ref(claim_id, "claim_id")
        version_id = _require(payload, "version_id")
        scope = _require(payload, "scope")
        languages = _require(payload, "languages", list)
        network_conditions = _require(payload, "network_conditions", dict)
        infrastructure_conditions = _require(
            payload, "infrastructure_conditions", dict
        )
        limitations = payload.get("limitations", "")
        if not isinstance(limitations, str):
            raise DomainError("invalid_field", "limitations 必须是字符串")
        with self.connect() as conn:
            self._require_role(conn, project_id, party, "supplier")
            owned = conn.execute(
                "SELECT 1 FROM system_versions"
                " WHERE version_id=? AND project_id=?",
                (version_id, project_id),
            ).fetchone()
            if owned is None:
                raise DomainError("not_found", "系统版本不存在", 404)
            try:
                conn.execute(
                    "INSERT INTO applicability_claims"
                    "(claim_id, version_id, scope, languages, network_conditions,"
                    " infrastructure_conditions, limitations, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        claim_id,
                        version_id,
                        scope,
                        _json_dumps(languages),
                        _json_dumps(network_conditions),
                        _json_dumps(infrastructure_conditions),
                        limitations,
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "适用声明编号已存在", 409) from None
        return {"claim_id": claim_id, "version_id": version_id}

    # ---- 里程碑与指标约定 ---------------------------------------------

    def create_milestone(
        self, project_id: str, payload: dict[str, Any], party: dict[str, Any]
    ) -> dict[str, Any]:
        milestone_id = _require(payload, "milestone_id")
        _check_ref(milestone_id, "milestone_id")
        milestone_ref = _require(payload, "milestone_ref")
        title = _require(payload, "title")
        stage = _require(payload, "stage")
        if stage not in STAGES:
            raise DomainError("invalid_field", f"stage 必须是 {STAGES} 之一")
        with self.connect() as conn:
            self._require_member(conn, project_id, party)
            try:
                conn.execute(
                    "INSERT INTO milestones"
                    "(milestone_id, project_id, milestone_ref, title, stage,"
                    " created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        milestone_id,
                        project_id,
                        milestone_ref,
                        title,
                        stage,
                        party["party_id"],
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "里程碑编号或引用已存在", 409) from None
        return {"milestone_id": milestone_id, "stage": stage}

    def add_metric(
        self, project_id: str, milestone_id: str, payload: dict[str, Any],
        party: dict[str, Any],
    ) -> dict[str, Any]:
        metric_id = _require(payload, "metric_id")
        _check_ref(metric_id, "metric_id")
        metric_key = _require(payload, "metric_key")
        name = _require(payload, "name")
        comparison = _require(payload, "comparison")
        if comparison not in ("gte", "lte", "eq"):
            raise DomainError("invalid_field", "comparison 必须是 gte/lte/eq")
        threshold = payload.get("threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            raise DomainError("invalid_field", "threshold 必须是数字")
        unit = payload.get("unit", "")
        if not isinstance(unit, str):
            raise DomainError("invalid_field", "unit 必须是字符串")
        with self.connect() as conn:
            self._require_member(conn, project_id, party)
            if not self._milestone_exists(conn, project_id, milestone_id):
                raise DomainError("not_found", "里程碑不存在", 404)
            try:
                conn.execute(
                    "INSERT INTO milestone_metrics"
                    "(metric_id, milestone_id, metric_key, name, comparison,"
                    " threshold, unit, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        metric_id,
                        milestone_id,
                        metric_key,
                        name,
                        comparison,
                        float(threshold),
                        unit,
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "指标编号或键已存在", 409) from None
        return {"metric_id": metric_id}

    def consent_metric(
        self,
        project_id: str,
        milestone_id: str,
        metric_id: str,
        party: dict[str, Any],
    ) -> dict[str, Any]:
        with self.connect() as conn:
            member = self._require_member(conn, project_id, party)
            if not self._milestone_exists(conn, project_id, milestone_id):
                raise DomainError("not_found", "里程碑不存在", 404)
            if conn.execute(
                "SELECT 1 FROM milestone_metrics"
                " WHERE metric_id=? AND milestone_id=?",
                (metric_id, milestone_id),
            ).fetchone() is None:
                raise DomainError("not_found", "指标不存在", 404)
            try:
                conn.execute(
                    "INSERT INTO metric_consents"
                    "(metric_id, party_id, role, consented_at) VALUES (?,?,?,?)",
                    (metric_id, party["party_id"], member["role"], now_iso()),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "已确认过该指标", 409) from None
        return {"metric_id": metric_id, "party_id": party["party_id"]}

    @staticmethod
    def _milestone_exists(
        conn: sqlite3.Connection, project_id: str, milestone_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT 1 FROM milestones WHERE milestone_id=? AND project_id=?",
            (milestone_id, project_id),
        ).fetchone()

    def _metrics_agreed(
        self, conn: sqlite3.Connection, milestone_id: str
    ) -> list[sqlite3.Row]:
        rows = conn.execute(
            "SELECT m.metric_id, m.metric_key, m.name, m.comparison, m.threshold,"
            " m.unit, COUNT(DISTINCT c.role) AS role_count"
            " FROM milestone_metrics m"
            " LEFT JOIN metric_consents c ON c.metric_id=m.metric_id"
            " WHERE m.milestone_id=? GROUP BY m.metric_id",
            (milestone_id,),
        ).fetchall()
        return rows

    # ---- 验收轮次与结论 -----------------------------------------------

    def open_round(
        self, project_id: str, milestone_id: str, payload: dict[str, Any],
        party: dict[str, Any],
    ) -> dict[str, Any]:
        round_id = _require(payload, "round_id")
        _check_ref(round_id, "round_id")
        revision = _require(payload, "revision", int)
        if isinstance(revision, bool) or revision < 1:
            raise DomainError("invalid_field", "revision 必须是不小于 1 的整数")
        with self.connect() as conn:
            self._require_member(conn, project_id, party)
            milestone = conn.execute(
                "SELECT milestone_id FROM milestones"
                " WHERE milestone_id=? AND project_id=?",
                (milestone_id, project_id),
            ).fetchone()
            if milestone is None:
                raise DomainError("not_found", "里程碑不存在", 404)
            metrics = self._metrics_agreed(conn, milestone_id)
            if not metrics:
                raise DomainError(
                    "metrics_not_agreed",
                    "里程碑尚未约定任何指标，不能开启验收",
                    409,
                )
            missing = [m["metric_key"] for m in metrics if m["role_count"] < 2]
            if missing:
                raise DomainError(
                    "metrics_not_agreed",
                    f"指标尚缺其中一方确认：{', '.join(missing)}",
                    409,
                )
            if conn.execute(
                "SELECT 1 FROM system_versions WHERE project_id=? AND revision=?",
                (project_id, revision),
            ).fetchone() is None:
                raise DomainError("invalid_field", "该系统版本不存在或不属于本项目")
            open_row = conn.execute(
                "SELECT 1 FROM acceptance_rounds"
                " WHERE milestone_id=? AND status='open'",
                (milestone_id,),
            ).fetchone()
            if open_row is not None:
                raise DomainError("conflict", "已有进行中的轮次，请先关闭", 409)
            round_no = conn.execute(
                "SELECT COALESCE(MAX(round_no),0)+1 FROM acceptance_rounds"
                " WHERE milestone_id=?",
                (milestone_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO acceptance_rounds"
                "(round_id, project_id, milestone_id, round_no, revision, status,"
                " opened_by, opened_at) VALUES (?,?,?,?,?, 'open', ?,?)",
                (
                    round_id,
                    project_id,
                    milestone_id,
                    round_no,
                    revision,
                    party["party_id"],
                    now_iso(),
                ),
            )
        return {"round_id": round_id, "round_no": round_no, "revision": revision}

    def submit_conclusion(
        self, project_id: str, round_id: str, payload: dict[str, Any],
        party: dict[str, Any],
    ) -> dict[str, Any]:
        conclusion_id = _require(payload, "conclusion_id")
        _check_ref(conclusion_id, "conclusion_id")
        measurements = _require(payload, "measurements", dict)
        signatures = payload.get("signatures", [])
        if not isinstance(signatures, list):
            raise DomainError("invalid_field", "signatures 必须是数组")

        with self.connect() as conn:
            self._require_member(conn, project_id, party)
            round_row = conn.execute(
                "SELECT round_id, milestone_id, round_no, status FROM acceptance_rounds"
                " WHERE round_id=? AND project_id=?",
                (round_id, project_id),
            ).fetchone()
            if round_row is None:
                raise DomainError("not_found", "轮次不存在", 404)
            if round_row["status"] != "open":
                raise DomainError(
                    "round_closed",
                    f"轮次状态为 {round_row['status']}，不能再提交结论；请新开轮次",
                    409,
                )

            metrics = self._metrics_agreed(conn, round_row["milestone_id"])
            missing = [m["metric_key"] for m in metrics if m["metric_key"] not in measurements]
            if missing:
                raise DomainError(
                    "invalid_field", f"实测数据缺少指标：{', '.join(missing)}"
                )
            passed = True
            evaluated: dict[str, dict[str, Any]] = {}
            for m in metrics:
                value = measurements[m["metric_key"]]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise DomainError(
                        "invalid_field", f"指标 {m['metric_key']} 的实测值必须是数字"
                    )
                if m["comparison"] == "gte":
                    ok = value >= m["threshold"]
                elif m["comparison"] == "lte":
                    ok = value <= m["threshold"]
                else:
                    ok = value == m["threshold"]
                passed = passed and ok
                evaluated[m["metric_key"]] = {
                    "value": value,
                    "threshold": m["threshold"],
                    "comparison": m["comparison"],
                    "met": ok,
                }
            extra = set(measurements) - {m["metric_key"] for m in metrics}
            if extra:
                raise DomainError("invalid_field", f"出现未约定的指标键：{', '.join(sorted(extra))}")

            # 校验签名：签名方必须是项目参与方；通过结论必须双方签。
            # 失败结论允许零签名（另一方可能拒签），提交人已记录在案。
            seen_roles: set[str] = set()
            cleaned_sigs: list[dict[str, str]] = []
            for sig in signatures:
                if not isinstance(sig, dict):
                    raise DomainError("invalid_field", "signatures 条目必须是对象")
                signer_id = _require(sig, "party_id")
                algo = _require(sig, "signature_algorithm")
                value = _require(sig, "signature_value")
                payload_digest = _require(sig, "signed_payload_digest")
                if not re.fullmatch(r"[0-9a-f]{64}", payload_digest):
                    raise DomainError(
                        "invalid_field", "signed_payload_digest 必须是 sha256"
                    )
                member = self._membership(conn, project_id, signer_id)
                if member is None:
                    raise DomainError("forbidden", f"签名方 {signer_id} 不是项目参与方", 403)
                if member["role"] in seen_roles:
                    raise DomainError("invalid_field", f"角色 {member['role']} 重复签名")
                seen_roles.add(member["role"])
                cleaned_sigs.append(
                    {
                        "party_id": signer_id,
                        "role": member["role"],
                        "signature_algorithm": algo,
                        "signature_value": value,
                        "signed_payload_digest": payload_digest,
                    }
                )
            if passed and seen_roles != {"local_institution", "supplier"}:
                raise DomainError(
                    "dual_signature_required",
                    "通过结论必须由当地机构与供应方双方签名",
                    409,
                )

            ts = now_iso()
            decision = "pass" if passed else "fail"
            round_status = "passed" if passed else "failed"
            conn.execute(
                "INSERT INTO conclusions"
                "(conclusion_id, round_id, decision, measurements, submitted_by, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    conclusion_id,
                    round_id,
                    decision,
                    _json_dumps(evaluated),
                    party["party_id"],
                    ts,
                ),
            )
            for sig in cleaned_sigs:
                conn.execute(
                    "INSERT INTO conclusion_signatures"
                    "(conclusion_id, party_id, role, signature_algorithm,"
                    " signature_value, signed_payload_digest, signed_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        conclusion_id,
                        sig["party_id"],
                        sig["role"],
                        sig["signature_algorithm"],
                        sig["signature_value"],
                        sig["signed_payload_digest"],
                        ts,
                    ),
                )
            conn.execute(
                "UPDATE acceptance_rounds SET status=?, closed_at=? WHERE round_id=?",
                (round_status, ts, round_id),
            )
        return {
            "conclusion_id": conclusion_id,
            "round_id": round_id,
            "decision": decision,
            "metrics": evaluated,
        }

    # ---- 证据摘要：仅参与方可见 ---------------------------------------

    def register_evidence(
        self, project_id: str, payload: dict[str, Any], party: dict[str, Any]
    ) -> dict[str, Any]:
        evidence_id = _require(payload, "evidence_id")
        _check_ref(evidence_id, "evidence_id")
        title = _require(payload, "title")
        digest = _require(payload, "digest")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DomainError("invalid_field", "digest 必须是 64 位十六进制 sha256")
        storage_ref = _require(payload, "storage_ref")
        round_id = payload.get("round_id")
        with self.connect() as conn:
            self._require_member(conn, project_id, party)
            if round_id is not None:
                if conn.execute(
                    "SELECT 1 FROM acceptance_rounds"
                    " WHERE round_id=? AND project_id=?",
                    (round_id, project_id),
                ).fetchone() is None:
                    raise DomainError("not_found", "轮次不存在", 404)
            try:
                conn.execute(
                    "INSERT INTO evidence_summaries"
                    "(evidence_id, project_id, round_id, title, digest_algorithm,"
                    " digest, storage_ref, submitted_by, submitted_at)"
                    " VALUES (?,?,?,?,'sha256',?,?,?,?)",
                    (
                        evidence_id,
                        project_id,
                        round_id,
                        title,
                        digest,
                        storage_ref,
                        party["party_id"],
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "证据编号已存在", 409) from None
        return {"evidence_id": evidence_id, "digest": digest}

    # ---- 读取视图 ------------------------------------------------------

    def project_detail(self, project_id: str, party: dict[str, Any]) -> dict[str, Any] | None:
        with self.connect() as conn:
            if self._membership(conn, project_id, party["party_id"]) is None:
                return None
            project = conn.execute(
                "SELECT project_id, title, created_at FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if project is None:
                return None
            data: dict[str, Any] = {
                "project_id": project["project_id"],
                "title": project["title"],
                "created_at": project["created_at"],
                "participants": [
                    dict(r)
                    for r in conn.execute(
                        "SELECT party_id, role, joined_at FROM project_participants"
                        " WHERE project_id=? ORDER BY joined_at",
                        (project_id,),
                    )
                ],
                "baselines": [
                    {
                        "baseline_id": r["baseline_id"],
                        "seq": r["seq"],
                        "requirements": json.loads(r["requirements"]),
                        "registered_by": r["registered_by"],
                        "created_at": r["created_at"],
                    }
                    for r in conn.execute(
                        "SELECT * FROM requirement_baselines WHERE project_id=?"
                        " ORDER BY seq",
                        (project_id,),
                    )
                ],
                "environments": [
                    {
                        "note_id": r["note_id"],
                        "languages": json.loads(r["languages"]),
                        "network": json.loads(r["network"]),
                        "infrastructure": json.loads(r["infrastructure"]),
                        "created_at": r["created_at"],
                    }
                    for r in conn.execute(
                        "SELECT * FROM environment_notes WHERE project_id=?"
                        " ORDER BY created_at",
                        (project_id,),
                    )
                ],
                "data_summaries": [
                    {
                        "summary_id": r["summary_id"],
                        "dataset_ref": r["dataset_ref"],
                        "digest_algorithm": r["digest_algorithm"],
                        "digest": r["digest"],
                        "row_count": r["row_count"],
                        "schema_note": json.loads(r["schema_note"]),
                        "created_at": r["created_at"],
                    }
                    for r in conn.execute(
                        "SELECT * FROM data_summaries WHERE project_id=?"
                        " ORDER BY created_at",
                        (project_id,),
                    )
                ],
                "versions": [],
                "milestones": [],
                "evidence": [
                    {
                        "evidence_id": r["evidence_id"],
                        "round_id": r["round_id"],
                        "title": r["title"],
                        "digest": r["digest"],
                        "storage_ref": r["storage_ref"],
                        "submitted_by": r["submitted_by"],
                        "submitted_at": r["submitted_at"],
                    }
                    for r in conn.execute(
                        "SELECT * FROM evidence_summaries WHERE project_id=?"
                        " ORDER BY submitted_at",
                        (project_id,),
                    )
                ],
            }
            for v in conn.execute(
                "SELECT * FROM system_versions WHERE project_id=? ORDER BY revision",
                (project_id,),
            ):
                claims = [
                    {
                        "claim_id": c["claim_id"],
                        "scope": c["scope"],
                        "languages": json.loads(c["languages"]),
                        "network_conditions": json.loads(c["network_conditions"]),
                        "infrastructure_conditions": json.loads(
                            c["infrastructure_conditions"]
                        ),
                        "limitations": c["limitations"],
                    }
                    for c in conn.execute(
                        "SELECT * FROM applicability_claims WHERE version_id=?",
                        (v["version_id"],),
                    )
                ]
                data["versions"].append(
                    {
                        "version_id": v["version_id"],
                        "revision": v["revision"],
                        "label": v["label"],
                        "release_note": v["release_note"],
                        "created_at": v["created_at"],
                        "claims": claims,
                    }
                )
            for ms in conn.execute(
                "SELECT * FROM milestones WHERE project_id=? ORDER BY created_at",
                (project_id,),
            ):
                metrics = []
                for m in conn.execute(
                    "SELECT * FROM milestone_metrics WHERE milestone_id=?",
                    (ms["milestone_id"],),
                ):
                    consents = [
                        {"party_id": c["party_id"], "role": c["role"],
                         "consented_at": c["consented_at"]}
                        for c in conn.execute(
                            "SELECT * FROM metric_consents WHERE metric_id=?",
                            (m["metric_id"],),
                        )
                    ]
                    metrics.append(
                        {
                            "metric_id": m["metric_id"],
                            "metric_key": m["metric_key"],
                            "name": m["name"],
                            "comparison": m["comparison"],
                            "threshold": m["threshold"],
                            "unit": m["unit"],
                            "consents": consents,
                            "agreed": len({c["role"] for c in consents}) == 2,
                        }
                    )
                rounds = []
                for rr in conn.execute(
                    "SELECT * FROM acceptance_rounds WHERE milestone_id=?"
                    " ORDER BY round_no",
                    (ms["milestone_id"],),
                ):
                    conclusion = None
                    c_row = conn.execute(
                        "SELECT * FROM conclusions WHERE round_id=?",
                        (rr["round_id"],),
                    ).fetchone()
                    if c_row:
                        sigs = [
                            dict(s)
                            for s in conn.execute(
                                "SELECT party_id, role, signature_algorithm,"
                                " signature_value, signed_payload_digest, signed_at"
                                " FROM conclusion_signatures WHERE conclusion_id=?",
                                (c_row["conclusion_id"],),
                            )
                        ]
                        conclusion = {
                            "conclusion_id": c_row["conclusion_id"],
                            "decision": c_row["decision"],
                            "measurements": json.loads(c_row["measurements"]),
                            "signatures": sigs,
                            "submitted_by": c_row["submitted_by"],
                            "created_at": c_row["created_at"],
                        }
                    rounds.append(
                        {
                            "round_id": rr["round_id"],
                            "round_no": rr["round_no"],
                            "revision": rr["revision"],
                            "status": rr["status"],
                            "opened_at": rr["opened_at"],
                            "closed_at": rr["closed_at"],
                            "conclusion": conclusion,
                        }
                    )
                data["milestones"].append(
                    {
                        "milestone_id": ms["milestone_id"],
                        "milestone_ref": ms["milestone_ref"],
                        "title": ms["title"],
                        "stage": ms["stage"],
                        "metrics": metrics,
                        "rounds": rounds,
                    }
                )
        return data

    def portfolio_combo(self, party: dict[str, Any]) -> dict[str, Any]:
        """组合视图：展品 / 试点 / 部署 / 正式验收四栏严格分开。

        演示、试点、部署的进展永远不会计入“正式验收”栏；
        正式验收栏只收录已有双方签名通过轮次的里程碑。
        """
        buckets: dict[str, list[dict[str, Any]]] = {s: [] for s in STAGES}
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT m.milestone_id, m.milestone_ref, m.title, m.stage,"
                " p.project_id, p.title AS project_title"
                " FROM milestones m JOIN projects p ON p.project_id=m.project_id"
                " JOIN project_participants pp ON pp.project_id=p.project_id"
                " WHERE pp.party_id=?",
                (party["party_id"],),
            ).fetchall()
            for r in rows:
                latest = conn.execute(
                    "SELECT round_no, revision, status FROM acceptance_rounds"
                    " WHERE milestone_id=? ORDER BY round_no DESC LIMIT 1",
                    (r["milestone_id"],),
                ).fetchone()
                accepted = conn.execute(
                    "SELECT 1 FROM acceptance_rounds WHERE milestone_id=?"
                    " AND status='passed' LIMIT 1",
                    (r["milestone_id"],),
                ).fetchone()
                entry = {
                    "project_id": r["project_id"],
                    "project_title": r["project_title"],
                    "milestone_id": r["milestone_id"],
                    "milestone_ref": r["milestone_ref"],
                    "title": r["title"],
                    "latest_round": dict(latest) if latest else None,
                    "formally_accepted": accepted is not None
                    and r["stage"] == "formal_acceptance",
                }
                buckets[r["stage"]].append(entry)
        return {
            "party_id": party["party_id"],
            "columns": buckets,
            "counts": {stage: len(items) for stage, items in buckets.items()},
            "note": "展品/试点/部署的记录不计入正式验收；正式验收以双方签名的通过轮次为准",
        }
