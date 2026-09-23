
from tests.support import HttpServerTest


class HealthTest(HttpServerTest):
    def test_health_ok_without_auth(self) -> None:
        status, body = self.api.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_unknown_path_404(self) -> None:
        status, body = self.api.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_v1_requires_token(self) -> None:
        status, body = self.api.request("GET", "/v1/projects")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
