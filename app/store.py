"""验收平台业务存储层。

核心规则：
* 库里只保存受控引用与 sha256 摘要，任何疑似原始业务数据的字段一律拒收；
* 里程碑指标必须由当地机构与供应方双方同意后，才能提交验收结论；
* 轮次状态机：open -> passed/failed；失败后可在同一里程碑另开新轮次；
* 供应方登记新版本时，该项目下所有尚未完成（open）的轮次自动失效；
* 证据摘要、轮次详情只对项目相关参与方与管理者可见。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

STAGES = ("demo", "pilot", "deployment", "acceptance")
STAGE_TITLES = {
    "demo": "展品",
    "pilot": "试点",
    "deployment": "部署",
    "acceptance": "正式验收",
}
ROUND_STATUSES = ("open", "passed", "failed", "invalidated")

# 出现这些键名即视为夹带原始业务数据，拒绝写入。精确匹配，sample_count 之类不受影响。
RAW_FIELD_NAMES = {
    "raw", "raw_data", "payload", "records", "rows", "samples",
    "content", "trace", "pcap", "video", "image", "audio", "dataset",
    "trip_data", "travel_data", "plate", "id_card",
}


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_digest(payload: Any) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_sha256_hex(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def assert_no_raw_data(obj: Any, path: str = "$") -> None:
    """递归检查载荷，拒绝疑似原始业务数据的字段。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                continue
            if key.lower() in RAW_FIELD_NAMES:
                raise StoreError(
                    "raw_data_rejected",
                    f"平台不接收原始业务数据：字段 {path}.{key} 被拒收，"
                    "请改为当地受控引用与 sha256 摘要。",
                )
            assert_no_raw_data(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            assert_no_raw_data(item, f"{path}[{index}]")


def _require(value: Any, field: str, allowed_type: type | tuple[type, ...]) -> Any:
    if not isinstance(value, allowed_type) or (allowed_type is str and not value.strip()):
        raise StoreError("validation", f"字段 {field} 缺失或类型不符")
    return value


class Store:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 认证

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def authenticate(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        row = self.connection.execute(
            "SELECT * FROM parties WHERE token_hash = ?", (self.hash_token(token),)
        ).fetchone()
        return row

    def register_party(
        self,
        caller: sqlite3.Row | None,
        ref: str,
        name: str,
        role: str,
        contact_ref: str | None = None,
    ) -> dict[str, Any]:
        ref = _require(ref, "ref", str).strip()
        name = _require(name, "name", str).strip()
        if role not in ("institution", "supplier", "administrator"):
            raise StoreError("validation", "role 必须是 institution/supplier/administrator")
        with self._lock:
            total = self.connection.execute("SELECT COUNT(*) AS n FROM parties").fetchone()["n"]
            # 仅允许在系统尚无任何参与方时自助登记首个管理者
            if total == 0:
                if role != "administrator":
                    raise StoreError("forbidden", "首个登记方必须是 administrator")
            elif caller is None or caller["role"] != "administrator":
                raise StoreError("forbidden", "只有管理者可以登记参与方")
            if self.connection.execute(
                "SELECT 1 FROM parties WHERE ref = ?", (ref,)
            ).fetchone():
                raise StoreError("conflict", f"参与方引用 {ref} 已存在")
            token = secrets.token_urlsafe(24)
            self.connection.execute(
                "INSERT INTO parties(ref, name, role, contact_ref, token_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ref, name, role, contact_ref, self.hash_token(token), now_iso()),
            )
            self.connection.commit()
        return {"ref": ref, "name": name, "role": role, "token": token}

    def list_parties(self, caller: sqlite3.Row) -> list[dict[str, Any]]:
        self._require_admin(caller)
        rows = self.connection.execute("SELECT ref, name, role, contact_ref, created_at FROM parties").fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- 项目

    def _require_admin(self, caller: sqlite3.Row | None) -> sqlite3.Row:
        if caller is None or caller["role"] != "administrator":
            raise StoreError("forbidden", "需要管理者权限")
        return caller

    def _load_project(self, project_ref: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE project_ref = ?", (project_ref,)
        ).fetchone()
        if row is None:
            raise StoreError("not_found", f"项目 {project_ref} 不存在")
        return row

    def _require_member(
        self, caller: sqlite3.Row | None, project: sqlite3.Row
    ) -> sqlite3.Row:
        if caller is None:
            raise StoreError("forbidden", "缺少身份凭证")
        if caller["role"] == "administrator":
            return caller
        if caller["ref"] not in (project["institution_ref"], project["supplier_ref"]):
            raise StoreError("forbidden", "该资源只对项目相关参与方可见")
        return caller

    def register_project(
        self,
        caller: sqlite3.Row,
        project_ref: str,
        name: str,
        domain: str,
        stage: str,
        institution_ref: str,
        supplier_ref: str,
    ) -> dict[str, Any]:
        self._require_admin(caller)
        project_ref = _require(project_ref, "project_ref", str).strip()
        name = _require(name, "name", str).strip()
        domain = _require(domain, "domain", str).strip()
        if stage not in STAGES:
            raise StoreError("validation", f"stage 必须是 {STAGES} 之一")
        if institution_ref == supplier_ref:
            raise StoreError("validation", "当地机构与供应方不能是同一参与方")
        for ref, want_role in (
            (institution_ref, "institution"),
            (supplier_ref, "supplier"),
        ):
            row = self.connection.execute(
                "SELECT role FROM parties WHERE ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise StoreError("validation", f"参与方 {ref} 未登记")
            if row["role"] != want_role:
                raise StoreError("validation", f"参与方 {ref} 的角色应为 {want_role}")
        with self._lock:
            try:
                self.connection.execute(
                    "INSERT INTO projects(project_ref, name, domain, stage,"
                    " institution_ref, supplier_ref, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (project_ref, name, domain, stage,
                     institution_ref, supplier_ref, now_iso()),
                )
                self.connection.commit()
            except sqlite3.IntegrityError:
                raise StoreError("conflict", f"项目 {project_ref} 已存在")
        return self.get_project(caller, project_ref)

    def list_projects(
        self, caller: sqlite3.Row, stage: str | None = None
    ) -> list[dict[str, Any]]:
        if stage is not None and stage not in STAGES:
            raise StoreError("validation", f"stage 必须是 {STAGES} 之一")
        sql = "SELECT * FROM projects"
        clauses: list[str] = []
        params: list[Any] = []
        if caller["role"] != "administrator":
            clauses.append("(institution_ref = ? OR supplier_ref = ?)")
            params.extend([caller["ref"], caller["ref"]])
        if stage:
            clauses.append("stage = ?")
            params.append(stage)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY project_ref"
        rows = self.connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, caller: sqlite3.Row, project_ref: str) -> dict[str, Any]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        return dict(project)

    # ------------------------------------------------------------ 系统版本

    def register_revision(
        self,
        caller: sqlite3.Row,
        project_ref: str,
        version_label: str,
        applicability: str,
    ) -> dict[str, Any]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        if caller["ref"] != project["supplier_ref"]:
            raise StoreError("forbidden", "只有供应方可以登记系统版本与适用声明")
        version_label = _require(version_label, "version_label", str).strip()
        applicability = _require(applicability, "applicability", str).strip()
        assert_no_raw_data({"version_label": version_label, "applicability": applicability})
        with self._lock:
            last = self.connection.execute(
                "SELECT COALESCE(MAX(revision_no), 0) AS n FROM system_revisions"
                " WHERE project_ref = ?",
                (project_ref,),
            ).fetchone()["n"]
            revision_no = last + 1
            cur = self.connection.execute(
                "INSERT INTO system_revisions(project_ref, revision_no, version_label,"
                " applicability, registered_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (project_ref, revision_no, version_label, applicability,
                 caller["ref"], now_iso()),
            )
            revision_id = cur.lastrowid
            # 升级版本：该项目下所有尚未完成的验收轮次立即失效
            invalidated = self.connection.execute(
                "UPDATE acceptance_rounds SET status = 'invalidated', invalidated_at = ?,"
                " invalidated_by_revision_id = ?"
                " WHERE project_ref = ? AND status = 'open'",
                (now_iso(), revision_id, project_ref),
            ).rowcount
            self.connection.commit()
        result = dict(self.connection.execute(
            "SELECT * FROM system_revisions WHERE id = ?", (revision_id,)
        ).fetchone())
        result["invalidated_open_rounds"] = invalidated
        return result

    def list_revisions(self, caller: sqlite3.Row, project_ref: str) -> list[dict[str, Any]]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        rows = self.connection.execute(
            "SELECT id, project_ref, revision_no, version_label, applicability,"
            " registered_by, created_at FROM system_revisions"
            " WHERE project_ref = ? ORDER BY revision_no",
            (project_ref,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------- 基线与环境说明

    def register_baseline(
        self,
        caller: sqlite3.Row,
        project_ref: str,
        requirements: list[dict[str, Any]],
        data_summary: dict[str, Any],
    ) -> dict[str, Any]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        if caller["ref"] != project["institution_ref"]:
            raise StoreError("forbidden", "只有当地机构可以登记需求基线与数据摘要")
        if not isinstance(requirements, list) or not requirements:
            raise StoreError("validation", "requirements 必须是非空列表")
        for item in requirements:
            if not isinstance(item, dict) or not str(item.get("code", "")).strip():
                raise StoreError("validation", "每条需求至少包含 code 与描述")
            _require(item.get("description"), "requirements[].description", str)
        if not isinstance(data_summary, dict) or not data_summary:
            raise StoreError("validation", "data_summary 必须是非空的聚合摘要对象")
        assert_no_raw_data({
            "requirements": requirements,
            "data_summary": data_summary,
        })
        digest = canonical_digest(
            {"requirements": requirements, "data_summary": data_summary}
        )
        with self._lock:
            cur = self.connection.execute(
                "INSERT INTO baselines(project_ref, requirements, data_summary, digest,"
                " registered_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (project_ref, json.dumps(requirements, ensure_ascii=False),
                 json.dumps(data_summary, ensure_ascii=False), digest,
                 caller["ref"], now_iso()),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM baselines WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self._baseline_dict(row)

    def list_baselines(self, caller: sqlite3.Row, project_ref: str) -> list[dict[str, Any]]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        rows = self.connection.execute(
            "SELECT * FROM baselines WHERE project_ref = ? ORDER BY id", (project_ref,)
        ).fetchall()
        return [self._baseline_dict(row) for row in rows]

    @staticmethod
    def _baseline_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_ref": row["project_ref"],
            "requirements": json.loads(row["requirements"]),
            "data_summary": json.loads(row["data_summary"]),
            "digest": row["digest"],
            "registered_by": row["registered_by"],
            "created_at": row["created_at"],
        }

    def register_environment(
        self,
        caller: sqlite3.Row,
        project_ref: str,
        language: str,
        network: str,
        infrastructure: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        if caller["ref"] != project["institution_ref"]:
            raise StoreError("forbidden", "只有当地机构可以登记现场环境说明")
        language = _require(language, "language", str).strip()
        network = _require(network, "network", str).strip()
        infrastructure = _require(infrastructure, "infrastructure", str).strip()
        assert_no_raw_data({
            "language": language,
            "network": network,
            "infrastructure": infrastructure,
            "notes": notes,
        })
        digest = canonical_digest(
            {"language": language, "network": network,
             "infrastructure": infrastructure, "notes": notes}
        )
        with self._lock:
            cur = self.connection.execute(
                "INSERT INTO environments(project_ref, language, network, infrastructure,"
                " notes, digest, registered_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_ref, language, network, infrastructure, notes,
                 digest, caller["ref"], now_iso()),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM environments WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def list_environments(self, caller: sqlite3.Row, project_ref: str) -> list[dict[str, Any]]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        rows = self.connection.execute(
            "SELECT * FROM environments WHERE project_ref = ? ORDER BY id", (project_ref,)
        ).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------- 里程碑与指标

    def create_milestone(
        self, caller: sqlite3.Row, project_ref: str, code: str, title: str
    ) -> dict[str, Any]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        code = _require(code, "code", str).strip()
        title = _require(title, "title", str).strip()
        with self._lock:
            try:
                cur = self.connection.execute(
                    "INSERT INTO milestones(project_ref, code, title, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (project_ref, code, title, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise StoreError("conflict", f"里程碑 {code} 已存在")
            self.connection.commit()
        return dict(self.connection.execute(
            "SELECT * FROM milestones WHERE id = ?", (cur.lastrowid,)
        ).fetchone())

    def list_milestones(self, caller: sqlite3.Row, project_ref: str) -> list[dict[str, Any]]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        rows = self.connection.execute(
            "SELECT * FROM milestones WHERE project_ref = ? ORDER BY id", (project_ref,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _load_milestone(self, milestone_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM milestones WHERE id = ?", (milestone_id,)
        ).fetchone()
        if row is None:
            raise StoreError("not_found", f"里程碑 {milestone_id} 不存在")
        return row

    def propose_metric(
        self,
        caller: sqlite3.Row,
        milestone_id: int,
        metric_code: str,
        name: str,
        operator: str,
        threshold: float,
    ) -> dict[str, Any]:
        milestone = self._load_milestone(milestone_id)
        project = self._load_project(milestone["project_ref"])
        self._require_member(caller, project)
        metric_code = _require(metric_code, "metric_code", str).strip()
        name = _require(name, "name", str).strip()
        if operator not in (">=", "<=", ">", "<", "="):
            raise StoreError("validation", "operator 必须是 >=, <=, >, <, = 之一")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            raise StoreError("validation", "threshold 必须是数值")
        proposer_role = caller["role"]
        if proposer_role not in ("institution", "supplier"):
            raise StoreError("forbidden", "只有机构或供应方可以提出指标")
        stamps = {"institution": None, "supplier": None}
        stamps[proposer_role] = now_iso()  # 提出即视为提出方同意
        with self._lock:
            try:
                cur = self.connection.execute(
                    "INSERT INTO milestone_metrics(milestone_id, metric_code, name, operator,"
                    " threshold, proposed_by, institution_agreed_at, supplier_agreed_at,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (milestone_id, metric_code, name, operator, float(threshold),
                     caller["ref"], stamps["institution"], stamps["supplier"], now_iso()),
                )
            except sqlite3.IntegrityError:
                raise StoreError("conflict", f"指标 {metric_code} 已存在")
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM milestone_metrics WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self._metric_dict(row)

    def agree_metric(self, caller: sqlite3.Row, metric_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM milestone_metrics WHERE id = ?", (metric_id,)
        ).fetchone()
        if row is None:
            raise StoreError("not_found", f"指标 {metric_id} 不存在")
        milestone = self._load_milestone(row["milestone_id"])
        project = self._load_project(milestone["project_ref"])
        self._require_member(caller, project)
        if caller["role"] not in ("institution", "supplier"):
            raise StoreError("forbidden", "只有机构或供应方可以同意指标")
        column = f"{caller['role']}_agreed_at"
        if row[column] is not None:
            raise StoreError("conflict", "本方已经同意该指标")
        with self._lock:
            self.connection.execute(
                f"UPDATE milestone_metrics SET {column} = ? WHERE id = ?",
                (now_iso(), metric_id),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM milestone_metrics WHERE id = ?", (metric_id,)
            ).fetchone()
        return self._metric_dict(row)

    def list_metrics(self, caller: sqlite3.Row, milestone_id: int) -> list[dict[str, Any]]:
        milestone = self._load_milestone(milestone_id)
        project = self._load_project(milestone["project_ref"])
        self._require_member(caller, project)
        rows = self.connection.execute(
            "SELECT * FROM milestone_metrics WHERE milestone_id = ? ORDER BY id",
            (milestone_id,),
        ).fetchall()
        return [self._metric_dict(row) for row in rows]

    @staticmethod
    def _metric_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["agreed"] = bool(
            row["institution_agreed_at"] and row["supplier_agreed_at"]
        )
        return data

    # ---------------------------------------------------------------- 轮次

    def _load_round(self, round_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM acceptance_rounds WHERE id = ?", (round_id,)
        ).fetchone()
        if row is None:
            raise StoreError("not_found", f"验收轮次 {round_id} 不存在")
        return row

    def open_round(
        self,
        caller: sqlite3.Row,
        project_ref: str,
        milestone_code: str,
        base_revision_id: int,
    ) -> dict[str, Any]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        milestone = self.connection.execute(
            "SELECT * FROM milestones WHERE project_ref = ? AND code = ?",
            (project_ref, milestone_code),
        ).fetchone()
        if milestone is None:
            raise StoreError("not_found", f"里程碑 {milestone_code} 不存在")
        revision = self.connection.execute(
            "SELECT * FROM system_revisions WHERE id = ? AND project_ref = ?",
            (base_revision_id, project_ref),
        ).fetchone()
        if revision is None:
            raise StoreError("validation", "基准系统版本不属于该项目")
        with self._lock:
            last = self.connection.execute(
                "SELECT COALESCE(MAX(round_no), 0) AS n FROM acceptance_rounds"
                " WHERE milestone_id = ?",
                (milestone["id"],),
            ).fetchone()["n"]
            round_no = last + 1
            cur = self.connection.execute(
                "INSERT INTO acceptance_rounds(project_ref, milestone_id, round_no,"
                " base_revision_id, status, opened_by, created_at)"
                " VALUES (?, ?, ?, ?, 'open', ?, ?)",
                (project_ref, milestone["id"], round_no,
                 base_revision_id, caller["ref"], now_iso()),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM acceptance_rounds WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self._round_summary(row)

    def list_rounds(self, caller: sqlite3.Row, project_ref: str) -> list[dict[str, Any]]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        rows = self.connection.execute(
            "SELECT * FROM acceptance_rounds WHERE project_ref = ?"
            " ORDER BY milestone_id, round_no",
            (project_ref,),
        ).fetchall()
        return [self._round_summary(row) for row in rows]

    def get_round(self, caller: sqlite3.Row, round_id: int) -> dict[str, Any]:
        row = self._load_round(round_id)
        project = self._load_project(row["project_ref"])
        self._require_member(caller, project)
        result = self._round_full(row)
        result["evidence"] = self._list_evidence(row["id"])
        return result

    # ------------------------------------------------------------- 证据摘要

    def register_evidence(
        self,
        caller: sqlite3.Row,
        round_id: int,
        kind: str,
        label: str,
        digest: str,
        controlled_ref: str,
    ) -> dict[str, Any]:
        round_row = self._load_round(round_id)
        project = self._load_project(round_row["project_ref"])
        self._require_member(caller, project)
        kind = _require(kind, "kind", str).strip()
        label = _require(label, "label", str).strip()
        controlled_ref = _require(controlled_ref, "controlled_ref", str).strip()
        digest = _require(digest, "digest", str).strip().lower()
        if not is_sha256_hex(digest):
            raise StoreError("validation", "digest 必须是 64 位十六进制 sha256 摘要")
        if round_row["status"] != "open":
            raise StoreError(
                "conflict",
                f"轮次状态为 {round_row['status']}，不能再登记证据；失败或失效请另开轮次",
            )
        with self._lock:
            cur = self.connection.execute(
                "INSERT INTO evidence_items(round_id, kind, label, digest, controlled_ref,"
                " registered_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (round_id, kind, label, digest, controlled_ref,
                 caller["ref"], now_iso()),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM evidence_items WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def _list_evidence(self, round_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id, round_id, kind, label, digest, controlled_ref, registered_by,"
            " created_at FROM evidence_items WHERE round_id = ? ORDER BY id",
            (round_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- 结论

    _OPERATORS = {
        ">=": lambda v, t: v >= t,
        "<=": lambda v, t: v <= t,
        ">": lambda v, t: v > t,
        "<": lambda v, t: v < t,
        "=": lambda v, t: v == t,
    }

    def submit_conclusion(
        self, caller: sqlite3.Row, round_id: int, measured: dict[str, float]
    ) -> dict[str, Any]:
        round_row = self._load_round(round_id)
        project = self._load_project(round_row["project_ref"])
        self._require_member(caller, project)
        if round_row["status"] != "open":
            raise StoreError(
                "conflict",
                f"轮次状态为 {round_row['status']}，不能提交结论；失败重测请另开轮次",
            )
        if not isinstance(measured, dict) or not measured:
            raise StoreError("validation", "measured 必须是非空对象：指标代码 -> 实测值")
        metrics = self.connection.execute(
            "SELECT * FROM milestone_metrics WHERE milestone_id = ?",
            (round_row["milestone_id"],),
        ).fetchall()
        if not metrics:
            raise StoreError("conflict", "该里程碑尚未约定任何指标，不能提交结论")
        by_code = {m["metric_code"]: m for m in metrics}
        missing = set(by_code) - set(measured)
        if missing:
            raise StoreError("validation", f"缺少指标实测值：{sorted(missing)}")
        unknown = set(measured) - set(by_code)
        if unknown:
            raise StoreError("validation", f"未知指标代码：{sorted(unknown)}")
        # 关键闸门：所有指标必须已由双方同意
        unresolved = [
            m["metric_code"]
            for m in metrics
            if not m["institution_agreed_at"] or not m["supplier_agreed_at"]
        ]
        if unresolved:
            raise StoreError(
                "metric_not_agreed",
                f"以下指标尚未经双方同意，不能提交结论：{unresolved}",
            )
        results: list[dict[str, Any]] = []
        all_passed = True
        for code in sorted(by_code):
            metric = by_code[code]
            value = measured[code]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise StoreError("validation", f"指标 {code} 的实测值必须是数值")
            passed = self._OPERATORS[metric["operator"]](float(value), metric["threshold"])
            all_passed = all_passed and passed
            results.append({
                "metric_code": code,
                "operator": metric["operator"],
                "threshold": metric["threshold"],
                "measured_value": float(value),
                "passed": passed,
            })
        decision = "passed" if all_passed else "failed"
        revision = self.connection.execute(
            "SELECT revision_no FROM system_revisions WHERE id = ?",
            (round_row["base_revision_id"],),
        ).fetchone()
        milestone = self._load_milestone(round_row["milestone_id"])
        concluded_at = now_iso()
        signature_payload = {
            "project_ref": project["project_ref"],
            "milestone_code": milestone["code"],
            "round_no": round_row["round_no"],
            "revision_no": revision["revision_no"],
            "results": results,
            "decision": decision,
            "concluded_at": concluded_at,
        }
        signature_digest = canonical_digest(signature_payload)
        with self._lock:
            for item in results:
                self.connection.execute(
                    "INSERT INTO round_metric_results(round_id, metric_id, measured_value,"
                    " passed) VALUES (?, ?, ?, ?)",
                    (round_id, by_code[item["metric_code"]]["id"],
                     item["measured_value"], int(item["passed"])),
                )
            self.connection.execute(
                "UPDATE acceptance_rounds SET status = ?, conclusion = ?,"
                " signature_digest = ?, submitted_by = ?, concluded_at = ? WHERE id = ?",
                (decision, json.dumps(results, ensure_ascii=False),
                 signature_digest, caller["ref"], concluded_at, round_id),
            )
            self.connection.commit()
        result = self._round_full(self._load_round(round_id))
        result["metric_results"] = results
        result["evidence"] = self._list_evidence(round_id)
        return result

    # ------------------------------------------------------------- 序列化

    def _round_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        data = {
            "id": row["id"],
            "project_ref": row["project_ref"],
            "milestone_id": row["milestone_id"],
            "round_no": row["round_no"],
            "base_revision_id": row["base_revision_id"],
            "status": row["status"],
            "opened_by": row["opened_by"],
            "created_at": row["created_at"],
            "concluded_at": row["concluded_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidated_by_revision_id": row["invalidated_by_revision_id"],
            "signature_digest": row["signature_digest"],
        }
        return data

    def _round_full(self, row: sqlite3.Row) -> dict[str, Any]:
        data = self._round_summary(row)
        data["submitted_by"] = row["submitted_by"]
        data["conclusion"] = json.loads(row["conclusion"]) if row["conclusion"] else None
        return data

    # ------------------------------------------------------- 项目摘要与组合视图

    def project_summary(self, caller: sqlite3.Row, project_ref: str) -> dict[str, Any]:
        project = self._load_project(project_ref)
        self._require_member(caller, project)
        return self._build_summary(project)

    def _build_summary(self, project: sqlite3.Row) -> dict[str, Any]:
        ref = project["project_ref"]
        latest_revision = self.connection.execute(
            "SELECT id, revision_no, version_label, applicability, registered_by, created_at"
            " FROM system_revisions WHERE project_ref = ? ORDER BY revision_no DESC LIMIT 1",
            (ref,),
        ).fetchone()
        latest_baseline = self.connection.execute(
            "SELECT id, digest, registered_by, created_at FROM baselines"
            " WHERE project_ref = ? ORDER BY id DESC LIMIT 1",
            (ref,),
        ).fetchone()
        latest_environment = self.connection.execute(
            "SELECT id, language, network, infrastructure, digest, registered_by, created_at"
            " FROM environments WHERE project_ref = ? ORDER BY id DESC LIMIT 1",
            (ref,),
        ).fetchone()
        milestones = []
        for ms in self.connection.execute(
            "SELECT * FROM milestones WHERE project_ref = ? ORDER BY id", (ref,)
        ).fetchall():
            metrics = [
                self._metric_dict(r)
                for r in self.connection.execute(
                    "SELECT * FROM milestone_metrics WHERE milestone_id = ? ORDER BY id",
                    (ms["id"],),
                ).fetchall()
            ]
            latest_round = self.connection.execute(
                "SELECT * FROM acceptance_rounds WHERE milestone_id = ?"
                " ORDER BY round_no DESC LIMIT 1",
                (ms["id"],),
            ).fetchone()
            milestones.append({
                "code": ms["code"],
                "title": ms["title"],
                "metrics": metrics,
                "metrics_agreed": all(m["agreed"] for m in metrics) if metrics else False,
                "latest_round": self._round_summary(latest_round) if latest_round else None,
            })
        return {
            "project": dict(project),
            "latest_revision": dict(latest_revision) if latest_revision else None,
            "latest_baseline": dict(latest_baseline) if latest_baseline else None,
            "latest_environment": dict(latest_environment) if latest_environment else None,
            "milestones": milestones,
            "rollup": self._rollup(ref),
        }

    def _rollup(self, project_ref: str) -> dict[str, int]:
        counts = {status: 0 for status in ROUND_STATUSES}
        for row in self.connection.execute(
            "SELECT status, COUNT(*) AS n FROM acceptance_rounds"
            " WHERE project_ref = ? GROUP BY status",
            (project_ref,),
        ).fetchall():
            counts[row["status"]] = row["n"]
        return counts

    def overview(self, caller: sqlite3.Row) -> dict[str, Any]:
        """组合视图：展品 / 试点 / 部署 / 正式验收严格分开。"""
        if caller["role"] != "administrator":
            rows = self.connection.execute(
                "SELECT * FROM projects WHERE institution_ref = ? OR supplier_ref = ?"
                " ORDER BY project_ref",
                (caller["ref"], caller["ref"]),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM projects ORDER BY project_ref"
            ).fetchall()
        buckets: dict[str, list[dict[str, Any]]] = {stage: [] for stage in STAGES}
        for project in rows:
            summary = self._build_summary(project)
            buckets[project["stage"]].append({
                "project_ref": project["project_ref"],
                "name": project["name"],
                "domain": project["domain"],
                "institution_ref": project["institution_ref"],
                "supplier_ref": project["supplier_ref"],
                "latest_revision_no": (
                    summary["latest_revision"]["revision_no"]
                    if summary["latest_revision"] else None
                ),
                "milestones": summary["milestones"],
                "rounds": summary["rollup"],
            })
        return {
            "stage_titles": STAGE_TITLES,
            "stages": {
                stage: {
                    "title": STAGE_TITLES[stage],
                    "projects": buckets[stage],
                    "project_count": len(buckets[stage]),
                }
                for stage in STAGES
            },
        }
