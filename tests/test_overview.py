"""组合视图：展品 / 试点 / 部署 / 正式验收严格分列，演示不会被误报为可交付。"""

from tests.support import HttpServerTest


class OverviewTest(HttpServerTest):
    def setUp(self) -> None:
        super().setUp()
        self.admin, self.inst, self.supp = self.bootstrap_parties()

    def _create(self, ref: str, stage: str, name: str) -> None:
        status, body = self.admin.request("POST", "/v1/projects", {
            "project_ref": ref, "name": name, "domain": "smart-traffic",
            "stage": stage,
            "institution_ref": "PARTNER-TH", "supplier_ref": "VENDOR-CN",
        })
        self.assertEqual(status, 201, body)

    def test_overview_separates_four_stages(self) -> None:
        self._create("PROJECT-DEMO-1", "demo", "北京展会演示")
        self._create("PROJECT-BKK", "pilot", "曼谷试点")
        self._create("PROJECT-BKK-DEP", "deployment", "曼谷部署")
        self._create("PROJECT-BKK-ACC", "acceptance", "曼谷正式验收")

        status, overview = self.admin.request("GET", "/v1/overview")
        self.assertEqual(status, 200, overview)
        stages = overview["stages"]
        self.assertEqual(
            [stages[s]["title"] for s in ("demo", "pilot", "deployment", "acceptance")],
            ["展品", "试点", "部署", "正式验收"],
        )
        self.assertEqual(
            stages["demo"]["projects"][0]["project_ref"], "PROJECT-DEMO-1"
        )
        self.assertEqual(stages["demo"]["project_count"], 1)
        self.assertEqual(stages["pilot"]["project_count"], 1)
        self.assertEqual(stages["deployment"]["project_count"], 1)
        self.assertEqual(stages["acceptance"]["project_count"], 1)

        # 每个阶段桶只含本阶段项目：展会演示绝不混进正式验收
        for stage, expected in (
            ("demo", "PROJECT-DEMO-1"),
            ("pilot", "PROJECT-BKK"),
            ("deployment", "PROJECT-BKK-DEP"),
            ("acceptance", "PROJECT-BKK-ACC"),
        ):
            refs = [p["project_ref"] for p in stages[stage]["projects"]]
            self.assertEqual(refs, [expected])

    def test_invalid_stage_rejected(self) -> None:
        status, body = self.admin.request("POST", "/v1/projects", {
            "project_ref": "X", "name": "x", "domain": "d", "stage": "roadshow",
            "institution_ref": "PARTNER-TH", "supplier_ref": "VENDOR-CN",
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation")

        status, body = self.admin.request("GET", "/v1/projects?stage=bogus")
        self.assertEqual(status, 400)

    def test_stage_filter(self) -> None:
        self._create("PROJECT-DEMO-1", "demo", "展会演示")
        self._create("PROJECT-BKK", "pilot", "曼谷试点")
        status, projects = self.supp.request("GET", "/v1/projects?stage=pilot")
        self.assertEqual(status, 200)
        self.assertEqual([p["project_ref"] for p in projects], ["PROJECT-BKK"])

    def test_overview_scoped_to_related_parties(self) -> None:
        self._create("PROJECT-BKK", "pilot", "曼谷试点")
        # 不相关参与方在组合视图中看不到该项目
        _, other_inst = self.admin.request("POST", "/v1/parties", {
            "ref": "PARTNER-LA", "name": "万象交通署", "role": "institution",
        })
        other = self.api.with_token(other_inst["token"])
        status, overview = other.request("GET", "/v1/overview")
        self.assertEqual(status, 200)
        for stage in overview["stages"].values():
            self.assertEqual(stage["projects"], [])

    def test_overview_reflects_round_status(self) -> None:
        self._create("PROJECT-BKK", "pilot", "曼谷试点")
        _, rev = self.supp.request("POST", "/v1/projects/PROJECT-BKK/revisions", {
            "version_label": "v1", "applicability": "泰语/弱网",
        })
        _, ms = self.inst.request("POST", "/v1/projects/PROJECT-BKK/milestones", {
            "code": "M1", "title": "现场验证",
        })
        _, metric = self.inst.request(
            "POST", f"/v1/milestones/{ms['id']}/metrics",
            {"metric_code": "acc", "name": "准确率", "operator": ">=", "threshold": 0.9},
        )
        self.supp.request("POST", f"/v1/metrics/{metric['id']}/agree")
        _, round1 = self.supp.request("POST", "/v1/projects/PROJECT-BKK/rounds", {
            "milestone_code": "M1", "base_revision_id": rev["id"],
        })
        self.supp.request("POST", f"/v1/rounds/{round1['id']}/conclusion",
                          {"measured": {"acc": 0.8}})

        status, overview = self.admin.request("GET", "/v1/overview")
        self.assertEqual(status, 200)
        project = overview["stages"]["pilot"]["projects"][0]
        self.assertEqual(project["rounds"], {"open": 0, "passed": 0, "failed": 1,
                                             "invalidated": 0})
        ms_view = project["milestones"][0]
        self.assertEqual(ms_view["latest_round"]["status"], "failed")
        self.assertTrue(ms_view["metrics_agreed"])

    def test_first_party_must_be_admin(self) -> None:
        # 空库自助登记非管理者应被拒绝
        status, body = self.api.request("POST", "/v1/parties", {
            "ref": "EVIL", "name": "x", "role": "supplier",
        })
        self.assertEqual(status, 403)
