"""HTTP 适配层：鉴权、路由与 JSON 报文，业务规则全部在 app.store。

启动前自动执行迁移；原始业务数据从不进入平台，报文大小也设上限。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

from app.store import DomainError, Store
from scripts.migrate import migrate

MAX_BODY_BYTES = 1 * 1024 * 1024

# 这些 POST 动作不接收请求体
NO_BODY_ACTIONS = frozenset({"consent_metric"})

PROJECTS = r"/projects/(?P<pid>[A-Za-z0-9._\-]+)"

ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("POST", re.compile(r"^/parties$"), "register_party"),
    ("GET", re.compile(r"^/me$"), "me"),
    ("POST", re.compile(r"^/projects$"), "create_project"),
    ("GET", re.compile(r"^/projects$"), "list_projects"),
    ("GET", re.compile(rf"^{PROJECTS}$"), "project_detail"),
    ("POST", re.compile(rf"^{PROJECTS}/participants$"), "add_participant"),
    ("POST", re.compile(rf"^{PROJECTS}/baselines$"), "register_baseline"),
    ("POST", re.compile(rf"^{PROJECTS}/environments$"), "register_environment"),
    ("POST", re.compile(rf"^{PROJECTS}/data-summaries$"), "register_data_summary"),
    ("POST", re.compile(rf"^{PROJECTS}/versions$"), "register_version"),
    ("POST", re.compile(rf"^{PROJECTS}/claims$"), "register_claim"),
    ("POST", re.compile(rf"^{PROJECTS}/milestones$"), "create_milestone"),
    (
        "POST",
        re.compile(rf"^{PROJECTS}/milestones/(?P<mid>[A-Za-z0-9._\-]+)/metrics$"),
        "add_metric",
    ),
    (
        "POST",
        re.compile(
            rf"^{PROJECTS}/milestones/(?P<mid>[A-Za-z0-9._\-]+)"
            r"/metrics/(?P<metric_id>[A-Za-z0-9._\-]+)/consent$"
        ),
        "consent_metric",
    ),
    (
        "POST",
        re.compile(rf"^{PROJECTS}/milestones/(?P<mid>[A-Za-z0-9._\-]+)/rounds$"),
        "open_round",
    ),
    (
        "POST",
        re.compile(rf"^{PROJECTS}/rounds/(?P<rid>[A-Za-z0-9._\-]+)/conclusion$"),
        "submit_conclusion",
    ),
    ("POST", re.compile(rf"^{PROJECTS}/evidence$"), "register_evidence"),
    ("GET", re.compile(r"^/portfolio/combo$"), "portfolio_combo"),
]


def build_store() -> Store:
    return Store(os.getenv("DATABASE_PATH", "data/app.sqlite3"))


class Handler(BaseHTTPRequestHandler):
    # 测试可注入临时 Store；生产进程首次请求时延迟构建（main 会先迁移）
    store: Store | None = None

    @classmethod
    def get_store(cls) -> Store:
        if cls.store is None:
            cls.store = build_store()
        return cls.store

    # ---- 框架辅助 ------------------------------------------------------

    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise DomainError("invalid_body", "请求体为空", 400)
        if length > MAX_BODY_BYTES:
            raise DomainError("body_too_large", "请求体超过 1MiB 上限", 413)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("invalid_body", "请求体不是合法 JSON", 400) from None
        if not isinstance(body, dict):
            raise DomainError("invalid_body", "请求体必须是 JSON 对象", 400)
        return body

    def _auth(self) -> dict[str, Any]:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise DomainError("unauthorized", "缺少 Bearer 令牌", 401)
        party = self.get_store().authenticate(header[len("Bearer ") :].strip())
        if party is None:
            raise DomainError("unauthorized", "令牌无效", 401)
        return party

    def _route(self) -> None:
        if self.command == "GET" and self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        body: dict[str, Any] | None = None
        for method, pattern, action in ROUTES:
            if method != self.command:
                continue
            match = pattern.match(self.path)
            if match is None:
                continue
            if method == "POST":
                # 纯动作端点（如指标确认）允许空请求体
                body = {} if action in NO_BODY_ACTIONS else self._read_body()
            party = None if action == "register_party" else self._auth()
            self._dispatch(action, match.groupdict(), body, party)
            return
        # 路径本身无任何方法可匹配 → 404；路径存在但方法不对 → 405
        known = any(p.match(self.path) for _, p, _ in ROUTES)
        self._send_json(405 if known else 404, {"error": "not_found" if not known else "method_not_allowed"})

    def _dispatch(
        self,
        action: str,
        params: dict[str, str],
        body: dict[str, Any] | None,
        party: dict[str, Any] | None,
    ) -> None:
        pid = unquote(params.get("pid", ""))
        store = self.get_store()
        if action == "register_party":
            result = store.register_party(
                body["party_id"], body["role"], body["label"], body["token"]
            )
            self._send_json(201, result)
        elif action == "me":
            self._send_json(200, party)
        elif action == "create_project":
            self._send_json(201, store.create_project(body["project_id"], body["title"], party))
        elif action == "list_projects":
            self._send_json(200, {"projects": store.list_projects(party)})
        elif action == "project_detail":
            detail = store.project_detail(pid, party)
            if detail is None:
                raise DomainError("forbidden", "项目不存在或无权查看", 403)
            self._send_json(200, detail)
        elif action == "add_participant":
            store.add_participant(pid, body["party_id"])
            self._send_json(201, {"project_id": pid, "party_id": body["party_id"]})
        elif action == "register_baseline":
            self._send_json(201, store.register_baseline(pid, body, party))
        elif action == "register_environment":
            self._send_json(201, store.register_environment(pid, body, party))
        elif action == "register_data_summary":
            self._send_json(201, store.register_data_summary(pid, body, party))
        elif action == "register_version":
            self._send_json(201, store.register_version(pid, body, party))
        elif action == "register_claim":
            self._send_json(201, store.register_claim(pid, body, party))
        elif action == "create_milestone":
            self._send_json(201, store.create_milestone(pid, body, party))
        elif action == "add_metric":
            self._send_json(201, store.add_metric(pid, unquote(params["mid"]), body, party))
        elif action == "consent_metric":
            self._send_json(
                201,
                store.consent_metric(
                    pid, unquote(params["mid"]), unquote(params["metric_id"]), party
                ),
            )
        elif action == "open_round":
            self._send_json(
                201, store.open_round(pid, unquote(params["mid"]), body, party)
            )
        elif action == "submit_conclusion":
            self._send_json(
                201, store.submit_conclusion(pid, unquote(params["rid"]), body, party)
            )
        elif action == "register_evidence":
            self._send_json(201, store.register_evidence(pid, body, party))
        elif action == "portfolio_combo":
            self._send_json(200, store.portfolio_combo(party))
        else:  # pragma: no cover - 路由表与分派保持一致
            self._send_json(404, {"error": "not_found"})

    # ---- BaseHTTPRequestHandler 入口 -----------------------------------

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        try:
            self._route()
        except DomainError as exc:
            self._send_json(exc.status, {"error": exc.code, "message": exc.message})
        except sqlite3.IntegrityError:
            self._send_json(409, {"error": "conflict", "message": "状态冲突或记录已存在"})
        except KeyError as exc:
            self._send_json(400, {"error": "invalid_field", "message": f"缺少字段 {exc.args[0]}"})
        except BrokenPipeError:  # pragma: no cover - 客户端提前断开
            pass

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    migrate()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
