"""测试支撑：在临时 SQLite 库上启动真实 HTTP 服务，返回 JSON 客户端。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.database import connect, migrate
from app.server import build_handler
from app.store import Store


class ApiClient:
    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url
        self.token = token

    def with_token(self, token: str | None) -> "ApiClient":
        return ApiClient(self.base_url, token)

    def request(self, method: str, path: str, body=None):
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = resp.read().decode("utf-8")
                return resp.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            payload = error.read().decode("utf-8")
            return error.code, json.loads(payload) if payload else None


class HttpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        Path(self.db_path).unlink(missing_ok=True)
        self.connection = connect(self.db_path)
        migrate(self.connection)
        self.store = Store(self.connection)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.store))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(f"http://127.0.0.1:{self.port}")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.connection.close()
        Path(self.db_path).unlink(missing_ok=True)

    # ---------------------------------------------------------- 业务夹具

    def bootstrap_parties(self):
        """登记管理者、当地机构、供应方，返回各自的客户端。"""
        _, admin_resp = self.api.request("POST", "/v1/parties", {
            "ref": "ADMIN", "name": "项目管理办公室", "role": "administrator",
        })
        admin = self.api.with_token(admin_resp["token"])
        _, inst = admin.request("POST", "/v1/parties", {
            "ref": "PARTNER-TH", "name": "曼谷交通数据署", "role": "institution",
        })
        _, supp = admin.request("POST", "/v1/parties", {
            "ref": "VENDOR-CN", "name": "智慧交通供应商", "role": "supplier",
        })
        return admin, self.api.with_token(inst["token"]), self.api.with_token(supp["token"])

    def create_project(self, admin, stage: str = "pilot", ref: str = "PROJECT-BKK"):
        status, body = admin.request("POST", "/v1/projects", {
            "project_ref": ref,
            "name": "曼谷智慧交通试点",
            "domain": "smart-traffic",
            "stage": stage,
            "institution_ref": "PARTNER-TH",
            "supplier_ref": "VENDOR-CN",
        })
        self.assertEqual(status, 201, body)
        return body
