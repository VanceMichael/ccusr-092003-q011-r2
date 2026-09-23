"""核心业务规则：失败重测、版本升级失效、指标同意闸门、证据可见性、原始数据拒收。"""

from tests.support import HttpServerTest


class RulesTest(HttpServerTest):
    def setUp(self) -> None:
        super().setUp()
        self.admin, self.inst, self.supp = self.bootstrap_parties()
        self.create_project(self.admin, stage="pilot")
        _, self.rev = self.supp.request("POST", "/v1/projects/PROJECT-BKK/revisions", {
            "version_label": "traffic-ai 2.3.1",
            "applicability": "泰语/弱网/路侧边缘",
        })
        _, self.ms = self.inst.request("POST", "/v1/projects/PROJECT-BKK/milestones", {
            "code": "M1", "title": "泰语识别现场验证",
        })
        _, self.metric = self.inst.request(
            "POST", f"/v1/milestones/{self.ms['id']}/metrics",
            {"metric_code": "th-acc", "name": "准确率", "operator": ">=", "threshold": 0.9},
        )
        _, self.metric = self.supp.request("POST", f"/v1/metrics/{self.metric['id']}/agree")

    def _open_round(self) -> dict:
        status, body = self.supp.request("POST", "/v1/projects/PROJECT-BKK/rounds", {
            "milestone_code": "M1",
            "base_revision_id": self.rev["id"],
        })
        self.assertEqual(status, 201, body)
        return body

    # ---------------------------------------------------------- 失败重测

    def test_failed_round_then_new_round_for_retest(self) -> None:
        round1 = self._open_round()
        status, conclusion = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/conclusion",
            {"measured": {"th-acc": 0.82}},
        )
        self.assertEqual(status, 200, conclusion)
        self.assertEqual(conclusion["status"], "failed")
        self.assertFalse(conclusion["metric_results"][0]["passed"])

        # 已结束轮次不能再补证据或改结论
        status, body = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/evidence",
            {"kind": "report", "label": "补证", "digest": "b" * 64,
             "controlled_ref": "X-1"},
        )
        self.assertEqual(status, 409)
        status, body = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/conclusion",
            {"measured": {"th-acc": 0.95}},
        )
        self.assertEqual(status, 409)

        # 失败重测另开第二轮，沿用同一里程碑与约定指标
        round2 = self._open_round()
        self.assertEqual(round2["round_no"], 2)
        self.assertEqual(round2["status"], "open")
        self.assertNotEqual(round1["id"], round2["id"])

        status, conclusion2 = self.supp.request(
            "POST", f"/v1/rounds/{round2['id']}/conclusion",
            {"measured": {"th-acc": 0.94}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(conclusion2["status"], "passed")

        # 两轮记录并存
        status, rounds = self.supp.request("GET", "/v1/projects/PROJECT-BKK/rounds")
        self.assertEqual(status, 200)
        self.assertEqual([r["round_no"] for r in rounds], [1, 2])
        self.assertEqual([r["status"] for r in rounds], ["failed", "passed"])

    # ------------------------------------------------------- 版本升级失效

    def test_new_revision_invalidates_open_rounds(self) -> None:
        round1 = self._open_round()
        status, rev2 = self.supp.request("POST", "/v1/projects/PROJECT-BKK/revisions", {
            "version_label": "traffic-ai 2.4.0",
            "applicability": "泰语/弱网/路侧边缘；新增老挝语",
        })
        self.assertEqual(status, 201, rev2)
        self.assertEqual(rev2["invalidated_open_rounds"], 1)
        self.assertEqual(rev2["revision_no"], 2)

        # 未完成的轮次被标记为 invalidated，不能再登记证据或提交结论
        status, detail = self.inst.request("GET", f"/v1/rounds/{round1['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "invalidated")
        self.assertEqual(detail["invalidated_by_revision_id"], rev2["id"])
        self.assertIsNotNone(detail["invalidated_at"])

        status, body = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/evidence",
            {"kind": "report", "label": "迟到证据", "digest": "c" * 64,
             "controlled_ref": "X-2"},
        )
        self.assertEqual(status, 409)
        status, body = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/conclusion",
            {"measured": {"th-acc": 0.99}},
        )
        self.assertEqual(status, 409)

        # 已完成的历史轮次不受新版本影响
        round2 = self._open_round()
        self.supp.request("POST", f"/v1/rounds/{round2['id']}/conclusion",
                          {"measured": {"th-acc": 0.95}})
        status, rev3 = self.supp.request("POST", "/v1/projects/PROJECT-BKK/revisions", {
            "version_label": "traffic-ai 2.5.0",
            "applicability": "泰语/老挝语/弱网",
        })
        self.assertEqual(status, 201, rev3)
        self.assertEqual(rev3["invalidated_open_rounds"], 0)
        status, detail = self.inst.request("GET", f"/v1/rounds/{round2['id']}")
        self.assertEqual(detail["status"], "passed")

    # --------------------------------------------------------- 同意闸门

    def test_conclusion_requires_mutual_agreement(self) -> None:
        _, metric2 = self.inst.request(
            "POST", f"/v1/milestones/{self.ms['id']}/metrics",
            {"metric_code": "latency", "name": "回传时延", "operator": "<=", "threshold": 2.0},
        )
        # latency 只有机构提出，供应方未同意
        round1 = self._open_round()
        status, body = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/conclusion",
            {"measured": {"th-acc": 0.93, "latency": 1.2}},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "metric_not_agreed")
        self.assertIn("latency", body["message"])

        # 供应方同意后才能提交
        self.assertEqual(self.supp.request("POST", f"/v1/metrics/{metric2['id']}/agree")[0], 200)
        status, conclusion = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/conclusion",
            {"measured": {"th-acc": 0.93, "latency": 1.2}},
        )
        self.assertEqual(status, 200, conclusion)
        self.assertEqual(conclusion["status"], "passed")

    def test_no_metrics_means_no_conclusion(self) -> None:
        _, ms2 = self.inst.request("POST", "/v1/projects/PROJECT-BKK/milestones", {
            "code": "M2", "title": "未约定指标的里程碑",
        })
        _, round1 = self.supp.request("POST", "/v1/projects/PROJECT-BKK/rounds", {
            "milestone_code": "M2",
            "base_revision_id": self.rev["id"],
        })
        status, body = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/conclusion",
            {"measured": {"anything": 1}},
        )
        self.assertEqual(status, 409)

    def test_cannot_agree_twice(self) -> None:
        # th-acc 在 setUp 中已被供应方同意
        status, body = self.supp.request("POST", f"/v1/metrics/{self.metric['id']}/agree")
        self.assertEqual(status, 409)

    # ------------------------------------------------------- 原始数据拒收

    def test_raw_data_fields_are_rejected(self) -> None:
        # 数据摘要中夹带原始记录键名 -> 422
        status, body = self.inst.request("POST", "/v1/projects/PROJECT-BKK/baselines", {
            "requirements": [{"code": "R1", "description": "d"}],
            "data_summary": {"sample_count": 3, "records": [{"plate": "1กข-234"}]},
        })
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "raw_data_rejected")

        status, body = self.inst.request("POST", "/v1/projects/PROJECT-BKK/baselines", {
            "requirements": [{"code": "R1", "description": "d"}],
            "data_summary": {"nested": {"raw_data": "..."}},
        })
        self.assertEqual(status, 422)

        # 证据接口只接受摘要与受控引用，夹带内容字段在入口即被拒收
        round1 = self._open_round()
        status, body = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/evidence",
            {"kind": "report", "label": "报告", "digest": "x" * 64,
             "controlled_ref": "X-3", "payload": "SHOULD-NOT-EXIST"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "raw_data_rejected")

    def test_evidence_requires_valid_sha256(self) -> None:
        round1 = self._open_round()
        status, body = self.supp.request(
            "POST", f"/v1/rounds/{round1['id']}/evidence",
            {"kind": "report", "label": "报告", "digest": "not-a-hash",
             "controlled_ref": "X-4"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation")

    # --------------------------------------------------------- 证据可见性

    def test_evidence_visible_only_to_related_parties(self) -> None:
        # 另一个不相关项目的机构与供应方
        _, foreign_inst = self.admin.request("POST", "/v1/parties", {
            "ref": "PARTNER-LA", "name": "万象交通署", "role": "institution",
        })
        _, foreign_supp = self.admin.request("POST", "/v1/parties", {
            "ref": "VENDOR-OTHER", "name": "其他供应商", "role": "supplier",
        })
        self.admin.request("POST", "/v1/projects", {
            "project_ref": "PROJECT-VTE", "name": "万象项目", "domain": "smart-traffic",
            "stage": "demo",
            "institution_ref": "PARTNER-LA", "supplier_ref": "VENDOR-OTHER",
        })
        round1 = self._open_round()
        self.supp.request("POST", f"/v1/rounds/{round1['id']}/evidence", {
            "kind": "report", "label": "内部报告", "digest": "d" * 64,
            "controlled_ref": "BKK-SECRET-1",
        })

        foreign_inst_api = self.api.with_token(foreign_inst["token"])
        foreign_supp_api = self.api.with_token(foreign_supp["token"])

        # 轮次详情、证据、项目摘要均不可见
        for client in (foreign_inst_api, foreign_supp_api):
            status, body = client.request("GET", f"/v1/rounds/{round1['id']}")
            self.assertEqual(status, 403, body)
            status, body = client.request("GET", "/v1/projects/PROJECT-BKK")
            self.assertEqual(status, 403)
            status, body = client.request("GET", "/v1/projects/PROJECT-BKK/summary")
            self.assertEqual(status, 403)
            status, projects = client.request("GET", "/v1/projects")
            self.assertEqual(status, 200)
            refs = [p["project_ref"] for p in projects]
            self.assertNotIn("PROJECT-BKK", refs)

        # 相关双方与管理者可见
        for client in (self.inst, self.supp, self.admin):
            status, body = client.request("GET", f"/v1/rounds/{round1['id']}")
            self.assertEqual(status, 200, body)
            self.assertEqual(body["evidence"][0]["controlled_ref"], "BKK-SECRET-1")
