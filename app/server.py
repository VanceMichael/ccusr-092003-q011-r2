"""HTTP 接口层：路由、Bearer 令牌认证、JSON 校验。

路由总览（详见 README）：
  POST   /v1/parties                         首个管理者自助登记，之后仅管理者
  GET    /v1/parties                         管理者
  POST   /v1/projects                        管理者
  GET    /v1/projects                        相关参与方/管理者，可用 ?stage= 过滤
  GET    /v1/projects/{ref}
  POST   /v1/projects/{ref}/revisions        供应方登记版本（会失效未完成轮次）
  GET    /v1/projects/{ref}/revisions
  POST   /v1/projects/{ref}/baselines        当地机构登记需求基线+数据摘要
  GET    /v1/projects/{ref}/baselines
  POST   /v1/projects/{ref}/environments     当地机构登记环境说明
  GET    /v1/projects/{ref}/environments
  POST   /v1/projects/{ref}/milestones       任一方
  GET    /v1/projects/{ref}/milestones
  POST   /v1/milestones/{id}/metrics         机构/供应方提出指标
  GET    /v1/milestones/{id}/metrics
  POST   /v1/metrics/{id}/agree              对方同意指标
  POST   /v1/projects/{ref}/rounds           开启验收轮次
  GET    /v1/projects/{ref}/rounds
  GET    /v1/rounds/{id}                     轮次详情含证据（仅相关方）
  POST   /v1/rounds/{id}/evidence            登记证据摘要+受控引用
  POST   /v1/rounds/{id}/conclusion          双方指标齐备后提交结论
  GET    /v1/projects/{ref}/summary          项目全量摘要
  GET    /v1/overview                        组合视图：四阶段分列
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .store import Store, StoreError, assert_no_raw_data

ERROR_STATUS = {
    "validation": 400,
    "raw_data_rejected": 422,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "metric_not_agreed": 409,
}

MAX_BODY_BYTES = 1 * 1024 * 1024


def build_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AcceptancePlatform/1.0"

        # ------------------------------------------------------------ 基础

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _caller(self):
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else None
            return store.authenticate(token)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise StoreError("validation", "请求体必须是 JSON 对象")
            if length > MAX_BODY_BYTES:
                raise StoreError("validation", "请求体过大")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise StoreError("validation", "请求体不是合法 JSON")
            if not isinstance(payload, dict):
                raise StoreError("validation", "请求体必须是 JSON 对象")
            # 入口统一拦截：任何写入请求夹带原始业务数据字段名一律拒收
            assert_no_raw_data(payload)
            return payload

        def _handle(self, method: str) -> None:
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            segments = [s for s in path.split("/") if s]
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            try:
                self._route(method, segments, query)
            except StoreError as error:
                self._send_json(
                    ERROR_STATUS.get(error.code, 400),
                    {"error": error.code, "message": str(error)},
                )
            except Exception:  # noqa: BLE001 - 兜底，保证未知异常也返回 JSON
                import logging
                logging.getLogger("acceptance").exception("未处理的服务异常")
                self._send_json(500, {"error": "internal", "message": "服务内部错误"})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, format: str, *args: object) -> None:
            return

        # ------------------------------------------------------------ 路由

        def _route(self, method: str, segs: list[str], query: dict[str, str]) -> None:
            if segs == ["health"]:
                if method != "GET":
                    raise StoreError("not_found", "路径不存在")
                self._send_json(200, {"status": "ok"})
                return
            if not segs or segs[0] != "v1":
                raise StoreError("not_found", "路径不存在")
            segs = segs[1:]

            # /parties
            if segs == ["parties"]:
                body = self._read_json() if method == "POST" else {}
                if method == "POST":
                    self._send_json(201, store.register_party(
                        self._caller(),
                        body.get("ref"), body.get("name"), body.get("role"),
                        body.get("contact_ref"),
                    ))
                elif method == "GET":
                    self._send_json(200, store.list_parties(self._require_auth()))
                else:
                    raise StoreError("not_found", "路径不存在")
                return

            # /projects
            if segs == ["projects"]:
                if method == "POST":
                    body = self._read_json()
                    self._send_json(201, store.register_project(
                        self._require_auth(),
                        body.get("project_ref"), body.get("name"), body.get("domain"),
                        body.get("stage"),
                        body.get("institution_ref"), body.get("supplier_ref"),
                    ))
                elif method == "GET":
                    self._send_json(200, store.list_projects(
                        self._require_auth(), query.get("stage")
                    ))
                else:
                    raise StoreError("not_found", "路径不存在")
                return

            # /overview
            if segs == ["overview"] and method == "GET":
                self._send_json(200, store.overview(self._require_auth()))
                return

            # /projects/{ref}/...
            if len(segs) >= 2 and segs[0] == "projects":
                self._project_routes(method, segs[1:])
                return

            # /milestones/{id}/metrics
            if len(segs) == 3 and segs[0] == "milestones" and segs[2] == "metrics":
                milestone_id = self._int_id(segs[1])
                if method == "POST":
                    body = self._read_json()
                    self._send_json(201, store.propose_metric(
                        self._require_auth(), milestone_id,
                        body.get("metric_code"), body.get("name"),
                        body.get("operator"), body.get("threshold"),
                    ))
                elif method == "GET":
                    self._send_json(200, store.list_metrics(
                        self._require_auth(), milestone_id
                    ))
                else:
                    raise StoreError("not_found", "路径不存在")
                return

            # /metrics/{id}/agree
            if len(segs) == 3 and segs[0] == "metrics" and segs[2] == "agree":
                if method != "POST":
                    raise StoreError("not_found", "路径不存在")
                self._send_json(200, store.agree_metric(
                    self._require_auth(), self._int_id(segs[1])
                ))
                return

            # /rounds/{id}/...
            if len(segs) >= 2 and segs[0] == "rounds":
                self._round_routes(method, segs[1:])
                return

            raise StoreError("not_found", "路径不存在")

        def _project_routes(self, method: str, segs: list[str]) -> None:
            ref = segs[0]
            caller = self._require_auth()
            if len(segs) == 1:
                if method != "GET":
                    raise StoreError("not_found", "路径不存在")
                self._send_json(200, store.get_project(caller, ref))
                return
            resource = segs[1]
            if resource == "revisions" and len(segs) == 2:
                if method == "POST":
                    body = self._read_json()
                    self._send_json(201, store.register_revision(
                        caller, ref,
                        body.get("version_label"), body.get("applicability"),
                    ))
                elif method == "GET":
                    self._send_json(200, store.list_revisions(caller, ref))
                else:
                    raise StoreError("not_found", "路径不存在")
                return
            if resource == "baselines" and len(segs) == 2:
                if method == "POST":
                    body = self._read_json()
                    self._send_json(201, store.register_baseline(
                        caller, ref,
                        body.get("requirements"), body.get("data_summary"),
                    ))
                elif method == "GET":
                    self._send_json(200, store.list_baselines(caller, ref))
                else:
                    raise StoreError("not_found", "路径不存在")
                return
            if resource == "environments" and len(segs) == 2:
                if method == "POST":
                    body = self._read_json()
                    self._send_json(201, store.register_environment(
                        caller, ref,
                        body.get("language"), body.get("network"),
                        body.get("infrastructure"), body.get("notes"),
                    ))
                elif method == "GET":
                    self._send_json(200, store.list_environments(caller, ref))
                else:
                    raise StoreError("not_found", "路径不存在")
                return
            if resource == "milestones" and len(segs) == 2:
                if method == "POST":
                    body = self._read_json()
                    self._send_json(201, store.create_milestone(
                        caller, ref, body.get("code"), body.get("title")
                    ))
                elif method == "GET":
                    self._send_json(200, store.list_milestones(caller, ref))
                else:
                    raise StoreError("not_found", "路径不存在")
                return
            if resource == "rounds" and len(segs) == 2:
                if method == "POST":
                    body = self._read_json()
                    self._send_json(201, store.open_round(
                        caller, ref,
                        body.get("milestone_code"), body.get("base_revision_id"),
                    ))
                elif method == "GET":
                    self._send_json(200, store.list_rounds(caller, ref))
                else:
                    raise StoreError("not_found", "路径不存在")
                return
            if resource == "summary" and len(segs) == 2 and method == "GET":
                self._send_json(200, store.project_summary(caller, ref))
                return
            raise StoreError("not_found", "路径不存在")

        def _round_routes(self, method: str, segs: list[str]) -> None:
            round_id = self._int_id(segs[0])
            caller = self._require_auth()
            if len(segs) == 1:
                if method != "GET":
                    raise StoreError("not_found", "路径不存在")
                self._send_json(200, store.get_round(caller, round_id))
                return
            if len(segs) == 2 and segs[1] == "evidence" and method == "POST":
                body = self._read_json()
                self._send_json(201, store.register_evidence(
                    caller, round_id,
                    body.get("kind"), body.get("label"),
                    body.get("digest"), body.get("controlled_ref"),
                ))
                return
            if len(segs) == 2 and segs[1] == "conclusion" and method == "POST":
                body = self._read_json()
                self._send_json(200, store.submit_conclusion(
                    caller, round_id, body.get("measured")
                ))
                return
            raise StoreError("not_found", "路径不存在")

        # ------------------------------------------------------------ 辅助

        def _require_auth(self):
            caller = self._caller()
            if caller is None:
                raise StoreError("forbidden", "缺少或无效的 Bearer 令牌")
            return caller

        @staticmethod
        def _int_id(value: str) -> int:
            if not re.fullmatch(r"\d+", value):
                raise StoreError("not_found", "路径不存在")
            return int(value)

    return Handler
