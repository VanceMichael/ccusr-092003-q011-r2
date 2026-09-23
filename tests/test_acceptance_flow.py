"""完整验收流程：登记 -> 版本/基线/环境 -> 指标双方同意 -> 轮次 -> 证据 -> 结论。"""

from tests.support import HttpServerTest


class AcceptanceFlowTest(HttpServerTest):
    def setUp(self) -> None:
        super().setUp()
        self.admin, self.inst, self.supp = self.bootstrap_parties()
        self.project = self.create_project(self.admin, stage="pilot")

    def test_full_passed_acceptance_flow(self) -> None:
        # 供应方登记系统版本与适用声明（针对泰语、现场网络与基础设施前提）
        status, rev = self.supp.request("POST", "/v1/projects/PROJECT-BKK/revisions", {
            "version_label": "traffic-ai 2.3.1",
            "applicability": "支持泰语车牌识别；适用 3G/4G 弱网回传；路侧 RSU 边缘部署",
        })
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["revision_no"], 1)

        # 当地机构登记需求基线与数据摘要（无原始记录，只有聚合统计）
        status, baseline = self.inst.request("POST", "/v1/projects/PROJECT-BKK/baselines", {
            "requirements": [
                {"code": "REQ-01", "description": "泰语路况语音播报准确率"},
                {"code": "REQ-02", "description": "弱网下事件回传时延"},
            ],
            "data_summary": {
                "sample_count": 12000,
                "time_range": "2026-08-01/2026-08-31",
                "aggregated_rate": {"peak_hour": 0.87},
            },
        })
        self.assertEqual(status, 201, baseline)
        self.assertEqual(len(baseline["digest"]), 64)

        # 当地机构登记现场环境说明
        status, env = self.inst.request("POST", "/v1/projects/PROJECT-BKK/environments", {
            "language": "th-TH",
            "network": "4G 为主，高峰时段丢包率约 6%",
            "infrastructure": "路侧机柜供电稳定，光纤回传仅覆盖主干道",
        })
        self.assertEqual(status, 201, env)
        self.assertEqual(env["language"], "th-TH")

        # 双方约定里程碑
        status, ms = self.inst.request("POST", "/v1/projects/PROJECT-BKK/milestones", {
            "code": "M1", "title": "泰语识别现场验证",
        })
        self.assertEqual(status, 201, ms)

        # 机构提出指标（提出即机构同意，尚待供应方同意）
        status, metric = self.inst.request("POST", f"/v1/milestones/{ms['id']}/metrics", {
            "metric_code": "th-acc",
            "name": "泰语事件识别准确率",
            "operator": ">=",
            "threshold": 0.9,
        })
        self.assertEqual(status, 201, metric)
        self.assertFalse(metric["agreed"])
        metric_id = metric["id"]

        # 供应方同意
        status, agreed = self.supp.request("POST", f"/v1/metrics/{metric_id}/agree")
        self.assertEqual(status, 200, agreed)
        self.assertTrue(agreed["agreed"])

        # 开启验收轮次
        status, round1 = self.supp.request("POST", "/v1/projects/PROJECT-BKK/rounds", {
            "milestone_code": "M1",
            "base_revision_id": rev["id"],
        })
        self.assertEqual(status, 201, round1)
        self.assertEqual(round1["round_no"], 1)
        self.assertEqual(round1["status"], "open")

        # 证据只登记 sha256 摘要与当地受控引用
        status, evidence = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/evidence",
            {
                "kind": "test-run-report",
                "label": "曼谷现场第一轮测试报告",
                "digest": "a" * 64,
                "controlled_ref": "BKK-DRDC-2026-09-001",
            },
        )
        self.assertEqual(status, 201, evidence)
        self.assertNotIn("raw", evidence)

        # 提交结论：实测达标
        status, conclusion = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/conclusion",
            {"measured": {"th-acc": 0.93}},
        )
        self.assertEqual(status, 200, conclusion)
        self.assertEqual(conclusion["status"], "passed")
        self.assertEqual(len(conclusion["signature_digest"]), 64)
        self.assertEqual(conclusion["metric_results"][0]["measured_value"], 0.93)
        self.assertTrue(conclusion["metric_results"][0]["passed"])

        # 轮次详情含证据
        status, detail = self.inst.request("GET", f"/v1/rounds/{round1['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["evidence"][0]["controlled_ref"], "BKK-DRDC-2026-09-001")

    def test_only_supplier_registers_revision(self) -> None:
        status, body = self.inst.request("POST", "/v1/projects/PROJECT-BKK/revisions", {
            "version_label": "x", "applicability": "y",
        })
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

    def test_only_institution_registers_baseline(self) -> None:
        status, body = self.supp.request("POST", "/v1/projects/PROJECT-BKK/baselines", {
            "requirements": [{"code": "R1", "description": "d"}],
            "data_summary": {"sample_count": 1},
        })
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
