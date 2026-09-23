"""端到端测试：在临时 SQLite 库上启动真实 HTTP 服务，逐条验证验收规则。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.main import Handler
from app.store import Store
from scripts.migrate import migrate

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # HTTP 服务只启动一次；每个用例换一个全新的数据库
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self) -> None:
        # 每个用例使用独立数据库、独立项目与独立令牌，避免相互干扰
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "test.sqlite3"
        migrate(db_path)
        Handler.store = Store(db_path)
        name = self._testMethodName
        self.project = f"p-{name}"
        self.local_pid = f"local-{name}"
        self.sup_pid = f"supplier-{name}"
        self.out_pid = f"outsider-{name}"
        self.local_tok = f"local-token-{name}"
        self.sup_tok = f"supplier-token-{name}"
        self.out_tok = f"outsider-token-{name}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict | None = None,
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # ---- 基础鉴权 ------------------------------------------------------

    def test_01_health_and_auth(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

        status, _ = self.request("GET", "/me")
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/me", token="wrong-token")
        self.assertEqual(status, 401)

        for pid, role, token in (
            ("local", "local_institution", self.local_tok),
            ("supplier", "supplier", self.sup_tok),
            ("outsider", "local_institution", self.out_tok),
        ):
            status, body = self.request("POST", "/parties", body={
                "party_id": f"{pid}-{self._testMethodName}",
                "role": role,
                "label": pid,
                "token": token,
            })
            self.assertEqual(status, 201, body)

        # 同一令牌不能重复登记
        status, body = self.request("POST", "/parties", body={
            "party_id": "dup", "role": "supplier", "label": "x",
            "token": self.sup_tok,
        })
        self.assertEqual(status, 409)

    def _parties(self) -> tuple[str, str, str]:
        name = self._testMethodName
        return f"local-{name}", f"supplier-{name}", f"outsider-{name}"

    def _tokens(self) -> tuple[str, str, str]:
        name = self._testMethodName
        return f"local-token-{name}", f"supplier-token-{name}", f"outsider-token-{name}"

    def _bootstrap_project(self) -> str:
        local, supplier, outsider = self._parties()
        local_tok, sup_tok, out_tok = self._tokens()
        self.request("POST", "/parties", body={
            "party_id": local, "role": "local_institution",
            "label": "当地机构", "token": local_tok})
        self.request("POST", "/parties", body={
            "party_id": supplier, "role": "supplier",
            "label": "供应方", "token": sup_tok})
        self.request("POST", "/parties", body={
            "party_id": outsider, "role": "local_institution",
            "label": "外部机构", "token": out_tok})
        status, body = self.request("POST", "/projects", local_tok, {
            "project_id": self.project, "title": "泰国智慧交通试点"})
        self.assertEqual(status, 201, body)
        status, body = self.request(
            "POST", f"/projects/{self.project}/participants", local_tok,
            {"party_id": supplier})
        self.assertEqual(status, 201, body)
        return self.project

    # ---- 原始数据不出境：只有摘要能登记 -------------------------------

    def test_02_only_summaries_accepted(self) -> None:
        self._bootstrap_project()
        status, body = self.request(
            "POST", f"/projects/{self.project}/data-summaries", self.local_tok, {
                "summary_id": "ds1",
                "dataset_ref": "BKK-TRANSIT-2026-09",
                "digest": "not-a-digest",
                "schema_note": {"fields": ["trip_count", "hour"]},
            })
        self.assertEqual(status, 400)

        raw_digest = sha256("10 万条原始出行记录留在当地机房")
        status, body = self.request(
            "POST", f"/projects/{self.project}/data-summaries", self.local_tok, {
                "summary_id": "ds1",
                "dataset_ref": "BKK-TRANSIT-2026-09",
                "digest": raw_digest,
                "row_count": 100_000,
                "schema_note": {"fields": ["trip_count", "hour"], "pii": "none"},
            })
        self.assertEqual(status, 201, body)
        self.assertEqual(body["digest"], raw_digest)

        # 供应方不能代替当地机构登记数据摘要
        status, _ = self.request(
            "POST", f"/projects/{self.project}/data-summaries", self.sup_tok, {
                "summary_id": "ds2", "dataset_ref": "x",
                "digest": raw_digest, "schema_note": {}})
        self.assertEqual(status, 403)

    def test_03_baseline_and_environment(self) -> None:
        self._bootstrap_project()
        status, body = self.request(
            "POST", f"/projects/{self.project}/baselines", self.local_tok, {
                "baseline_id": "bl1",
                "requirements": {
                    "language": "th",
                    "must_work_offline": True,
                    "accuracy_target": 0.95,
                }})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["seq"], 1)

        status, body = self.request(
            "POST", f"/projects/{self.project}/environments", self.local_tok, {
                "note_id": "env1",
                "languages": ["th", "en"],
                "network": {"uplink_mbps": 4, "outage_tolerated": True},
                "infrastructure": {"edge_nodes": 2, "camera_model": "local-A"},
            })
        self.assertEqual(status, 201, body)

        # 基线序号递增，且供应方无权登记
        status, _ = self.request(
            "POST", f"/projects/{self.project}/baselines", self.sup_tok,
            {"baseline_id": "blx", "requirements": {"x": 1}})
        self.assertEqual(status, 403)

    # ---- 指标未约定不能验收；双方确认后才行 ---------------------------

    def _agree_milestone(self, ms: str, stage: str, metrics: list[dict]) -> None:
        status, body = self.request(
            "POST", f"/projects/{self.project}/milestones", self.local_tok, {
                "milestone_id": ms, "milestone_ref": ms,
                "title": ms, "stage": stage})
        self.assertEqual(status, 201, body)
        for metric in metrics:
            status, body = self.request(
                "POST",
                f"/projects/{self.project}/milestones/{ms}/metrics",
                self.local_tok, metric)
            self.assertEqual(status, 201, body)
            for token in (self.local_tok, self.sup_tok):
                status, body = self.request(
                    "POST",
                    f"/projects/{self.project}/milestones/{ms}"
                    f"/metrics/{metric['metric_id']}/consent",
                    token)
                self.assertEqual(status, 201, body)

    def test_04_metrics_must_be_agreed_by_both_sides(self) -> None:
        self._bootstrap_project()
        self.request("POST", f"/projects/{self.project}/versions", self.sup_tok, {
            "version_id": "v1", "revision": 1, "label": "edge-1.0"})

        self.request("POST", f"/projects/{self.project}/milestones", self.local_tok, {
            "milestone_id": "fa", "milestone_ref": "FA-1",
            "title": "正式验收", "stage": "formal_acceptance"})
        self.request(
            "POST", f"/projects/{self.project}/milestones/fa/metrics", self.local_tok, {
                "metric_id": "acc", "metric_key": "accuracy",
                "name": "泰语场景准确率", "comparison": "gte",
                "threshold": 0.95, "unit": "ratio"})

        # 还没有任何一方确认 → 不能开轮
        status, body = self.request(
            "POST", f"/projects/{self.project}/milestones/fa/rounds", self.local_tok,
            {"round_id": "r0", "revision": 1})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "metrics_not_agreed")

        # 只有当地机构确认仍不够
        self.request(
            "POST",
            f"/projects/{self.project}/milestones/fa/metrics/acc/consent",
            self.local_tok)
        status, body = self.request(
            "POST", f"/projects/{self.project}/milestones/fa/rounds", self.local_tok,
            {"round_id": "r0", "revision": 1})
        self.assertEqual(status, 409)

        # 供应方确认后可以开轮
        status, _ = self.request(
            "POST",
            f"/projects/{self.project}/milestones/fa/metrics/acc/consent",
            self.sup_tok)
        self.assertEqual(status, 201)
        status, body = self.request(
            "POST", f"/projects/{self.project}/milestones/fa/rounds", self.local_tok,
            {"round_id": "r0", "revision": 1})
        self.assertEqual(status, 201, body)

        # 已有进行中的轮次 → 不能重复开启
        status, _ = self.request(
            "POST", f"/projects/{self.project}/milestones/fa/rounds", self.sup_tok,
            {"round_id": "r0b", "revision": 1})
        self.assertEqual(status, 409)

    # ---- 失败重测新开轮次 ----------------------------------------------

    def _dual_signatures(self) -> list[dict]:
        local, supplier, _ = self._parties()
        return [
            {"party_id": local, "signature_algorithm": "ed25519",
             "signature_value": "local-sig",
             "signed_payload_digest": sha256("conclusion-pass")},
            {"party_id": supplier, "signature_algorithm": "ed25519",
             "signature_value": "supplier-sig",
             "signed_payload_digest": sha256("conclusion-pass")},
        ]

    def test_05_failed_retest_opens_new_round(self) -> None:
        self._bootstrap_project()
        self.request("POST", f"/projects/{self.project}/versions", self.sup_tok, {
            "version_id": "v1", "revision": 1, "label": "edge-1.0"})
        self._agree_milestone("fa", "formal_acceptance", [
            {"metric_id": "acc", "metric_key": "accuracy", "name": "准确率",
             "comparison": "gte", "threshold": 0.95},
            {"metric_id": "lat", "metric_key": "latency_ms", "name": "延迟",
             "comparison": "lte", "threshold": 300, "unit": "ms"},
        ])

        self.request("POST", f"/projects/{self.project}/milestones/fa/rounds",
                     self.local_tok, {"round_id": "r1", "revision": 1})

        # 国内展会式高分在真实条件下不达标：准确率 0.88 < 0.95
        status, body = self.request(
            "POST", f"/projects/{self.project}/rounds/r1/conclusion", self.local_tok, {
                "conclusion_id": "c1",
                "measurements": {"accuracy": 0.88, "latency_ms": 250},
                "signatures": [],
            })
        self.assertEqual(status, 201, body)
        self.assertEqual(body["decision"], "fail")
        self.assertFalse(body["metrics"]["accuracy"]["met"])

        # 失败轮关闭，重测必须另开第 2 轮，历史保留
        status, body = self.request(
            "POST", f"/projects/{self.project}/milestones/fa/rounds", self.sup_tok,
            {"round_id": "r2", "revision": 1})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["round_no"], 2)

        # 通过结论只签一方 → 拒绝
        local, supplier, _ = self._parties()
        status, body = self.request(
            "POST", f"/projects/{self.project}/rounds/r2/conclusion", self.local_tok, {
                "conclusion_id": "c2",
                "measurements": {"accuracy": 0.97, "latency_ms": 210},
                "signatures": [{
                    "party_id": local, "signature_algorithm": "ed25519",
                    "signature_value": "only-local",
                    "signed_payload_digest": sha256("x")}],
            })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "dual_signature_required")

        # 已关闭的轮次不能再提交
        status, _ = self.request(
            "POST", f"/projects/{self.project}/rounds/r1/conclusion", self.local_tok, {
                "conclusion_id": "cx",
                "measurements": {"accuracy": 0.99, "latency_ms": 100},
                "signatures": self._dual_signatures()})
        self.assertEqual(status, 409)

    # ---- 版本升级作废旧轮次 --------------------------------------------

    def test_06_version_upgrade_supersedes_open_round(self) -> None:
        self._bootstrap_project()
        self.request("POST", f"/projects/{self.project}/versions", self.sup_tok, {
            "version_id": "v1", "revision": 1, "label": "edge-1.0"})
        self.request("POST", f"/projects/{self.project}/claims", self.sup_tok, {
            "claim_id": "claim1", "version_id": "v1",
            "scope": "曼谷试点路口", "languages": ["th"],
            "network_conditions": {"min_mbps": 4},
            "infrastructure_conditions": {"edge_nodes": 2},
            "limitations": "不支持离线超过 24h"})
        self._agree_milestone("fa", "formal_acceptance", [
            {"metric_id": "acc", "metric_key": "accuracy", "name": "准确率",
             "comparison": "gte", "threshold": 0.95}])

        self.request("POST", f"/projects/{self.project}/milestones/fa/rounds",
                     self.local_tok, {"round_id": "r1", "revision": 1})

        # 版本号必须递增
        status, body = self.request(
            "POST", f"/projects/{self.project}/versions", self.sup_tok, {
                "version_id": "vOld", "revision": 1, "label": "dup"})
        self.assertEqual(status, 409)
        # 当地机构不能登记版本
        status, _ = self.request(
            "POST", f"/projects/{self.project}/versions", self.local_tok, {
                "version_id": "v2x", "revision": 2, "label": "x"})
        self.assertEqual(status, 403)

        # 升级到 revision 2：尚未完成的 r1 立即失效
        status, body = self.request(
            "POST", f"/projects/{self.project}/versions", self.sup_tok, {
                "version_id": "v2", "revision": 2,
                "label": "edge-1.1", "release_note": "修复泰语分词"})
        self.assertEqual(status, 201, body)

        status, _ = self.request(
            "POST", f"/projects/{self.project}/rounds/r1/conclusion", self.local_tok, {
                "conclusion_id": "c1", "measurements": {"accuracy": 0.99},
                "signatures": self._dual_signatures()})
        self.assertEqual(status, 409)

        # 必须针对新版本重新开轮
        status, body = self.request(
            "POST", f"/projects/{self.project}/milestones/fa/rounds", self.sup_tok,
            {"round_id": "r2", "revision": 2})
        self.assertEqual(status, 201, body)
        status, body = self.request(
            "POST", f"/projects/{self.project}/rounds/r2/conclusion", self.local_tok, {
                "conclusion_id": "c2", "measurements": {"accuracy": 0.96},
                "signatures": self._dual_signatures()})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["decision"], "pass")

    # ---- 证据只对参与方可见 --------------------------------------------

    def test_07_evidence_visible_only_to_participants(self) -> None:
        self._bootstrap_project()
        digest = sha256("验收现场录屏与日志包")

        # 非参与方不能提交证据
        status, _ = self.request(
            "POST", f"/projects/{self.project}/evidence", self.out_tok, {
                "evidence_id": "e1", "title": "现场证据",
                "digest": digest, "storage_ref": "local-vault://e1"})
        self.assertEqual(status, 403)

        status, body = self.request(
            "POST", f"/projects/{self.project}/evidence", self.local_tok, {
                "evidence_id": "e1", "title": "现场证据",
                "digest": digest, "storage_ref": "local-vault://e1"})
        self.assertEqual(status, 201, body)

        # 非参与方连项目视图都拿不到（证据在其中）
        status, _ = self.request("GET", f"/projects/{self.project}", self.out_tok)
        self.assertEqual(status, 403)

        # 双方参与方可见，且内容只有摘要与受控引用
        for token in (self.local_tok, self.sup_tok):
            status, body = self.request(
                "GET", f"/projects/{self.project}", token)
            self.assertEqual(status, 200)
            evidence = body["evidence"]
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["digest"], digest)
            self.assertNotIn("raw", json.dumps(body, ensure_ascii=False))

        # 项目列表对外人也是空的
        status, body = self.request("GET", "/projects", self.out_tok)
        self.assertEqual(status, 200)
        self.assertNotIn(self.project, [p["project_id"] for p in body["projects"]])

    # ---- 组合视图：演示不会被报成正式交付 -----------------------------

    def test_08_combo_keeps_four_stages_apart(self) -> None:
        self._bootstrap_project()
        self.request("POST", f"/projects/{self.project}/versions", self.sup_tok, {
            "version_id": "v1", "revision": 1, "label": "edge-1.0"})

        # 展品（国内展会演示）也做到了指标全过
        self._agree_milestone("demo", "demonstration", [
            {"metric_id": "dacc", "metric_key": "accuracy", "name": "演示准确率",
             "comparison": "gte", "threshold": 0.9}])
        self.request("POST", f"/projects/{self.project}/milestones/demo/rounds",
                     self.local_tok, {"round_id": "dr1", "revision": 1})
        status, body = self.request(
            "POST", f"/projects/{self.project}/rounds/dr1/conclusion", self.local_tok, {
                "conclusion_id": "dc1", "measurements": {"accuracy": 0.99},
                "signatures": self._dual_signatures()})
        self.assertEqual(status, 201, body)

        # 正式验收：首次当地条件下失败，第二次通过
        self._agree_milestone("fa", "formal_acceptance", [
            {"metric_id": "facc", "metric_key": "accuracy", "name": "泰语准确率",
             "comparison": "gte", "threshold": 0.95}])
        self.request("POST", f"/projects/{self.project}/milestones/fa/rounds",
                     self.local_tok, {"round_id": "fr1", "revision": 1})
        self.request("POST", f"/projects/{self.project}/rounds/fr1/conclusion",
                     self.local_tok, {
                         "conclusion_id": "fc1",
                         "measurements": {"accuracy": 0.88},
                         "signatures": []})
        self.request("POST", f"/projects/{self.project}/milestones/fa/rounds",
                     self.sup_tok, {"round_id": "fr2", "revision": 1})
        status, body = self.request(
            "POST", f"/projects/{self.project}/rounds/fr2/conclusion", self.local_tok, {
                "conclusion_id": "fc2", "measurements": {"accuracy": 0.96},
                "signatures": self._dual_signatures()})
        self.assertEqual(status, 201, body)

        status, combo = self.request("GET", "/portfolio/combo", self.local_tok)
        self.assertEqual(status, 200)
        cols = combo["columns"]
        self.assertEqual(set(cols), {
            "demonstration", "pilot", "deployment", "formal_acceptance"})

        demo_entries = cols["demonstration"]
        demo_row = next(e for e in demo_entries if e["milestone_id"] == "demo")
        # 关键：演示即使通过，也绝不是“正式验收通过”
        self.assertFalse(demo_row["formally_accepted"])

        formal_rows = cols["formal_acceptance"]
        formal_row = next(e for e in formal_rows if e["milestone_id"] == "fa")
        self.assertTrue(formal_row["formally_accepted"])
        self.assertEqual(formal_row["latest_round"]["round_no"], 2)
        self.assertEqual(combo["counts"]["pilot"], 0)


if __name__ == "__main__":
    unittest.main()
