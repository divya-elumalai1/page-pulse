import httpx
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.main import app, cache, rate_limiter
from app.cache import TTLCache
from app.rate_limiter import RateLimiter


@pytest.fixture(autouse=True)
def reset_state():
    """Give every test a clean cache and rate limiter."""
    cache.clear()
    rate_limiter._buckets.clear()
    yield
    cache.clear()
    rate_limiter._buckets.clear()


client = TestClient(app)


def _mock_response(status_code=200, content=b"hello world", headers=None):
    headers = headers or {"server": "nginx", "content-type": "text/html"}
    req = httpx.Request("GET", "https://example.com")
    return httpx.Response(status_code=status_code, content=content, headers=headers, request=req)


class TestHealth:
    def test_health_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestValidation:
    def test_invalid_url_rejected(self):
        resp = client.post("/audit", json={"url": "not-a-url"})
        assert resp.status_code == 422  # FastAPI/pydantic validation error

    def test_missing_url_rejected(self):
        resp = client.post("/audit", json={})
        assert resp.status_code == 422


class TestAuditSuccess:
    @patch("app.audit.httpx.AsyncClient.get", new_callable=AsyncMock)
    def test_successful_audit_returns_expected_fields(self, mock_get):
        mock_get.return_value = _mock_response(status_code=200)
        resp = client.post("/audit", json={"url": "https://example.com"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status_code"] == 200
        assert body["from_cache"] is False
        assert body["server_header"] == "nginx"
        assert "response_time_ms" in body

    @patch("app.audit.httpx.AsyncClient.get", new_callable=AsyncMock)
    def test_target_4xx_is_still_a_valid_audit(self, mock_get):
        mock_get.return_value = _mock_response(status_code=404)
        resp = client.post("/audit", json={"url": "https://example.com/missing"})
        assert resp.status_code == 200
        assert resp.json()["status_code"] == 404


class TestCaching:
    @patch("app.audit.httpx.AsyncClient.get", new_callable=AsyncMock)
    def test_second_request_hits_cache(self, mock_get):
        mock_get.return_value = _mock_response()
        r1 = client.post("/audit", json={"url": "https://example.com/cached"})
        r2 = client.post("/audit", json={"url": "https://example.com/cached"})
        assert r1.json()["from_cache"] is False
        assert r2.json()["from_cache"] is True
        # underlying fetch should only have happened once
        assert mock_get.call_count == 1


class TestTimeoutAndErrors:
    @patch("app.audit.httpx.AsyncClient.get", new_callable=AsyncMock)
    def test_timeout_returns_502_with_structured_error(self, mock_get):
        mock_get.side_effect = httpx.TimeoutException("timed out")
        resp = client.post("/audit", json={"url": "https://slow.example.com"})
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"] == "TIMEOUT"
        assert "request_id" in body

    @patch("app.audit.httpx.AsyncClient.get", new_callable=AsyncMock)
    def test_connection_error_returns_502(self, mock_get):
        mock_get.side_effect = httpx.ConnectError("refused")
        resp = client.post("/audit", json={"url": "https://unreachable.example.com"})
        assert resp.status_code == 502
        assert resp.json()["error"] == "CONNECTION_FAILED"


class TestRateLimiting:
    @patch("app.audit.httpx.AsyncClient.get", new_callable=AsyncMock)
    def test_rate_limit_trips_after_threshold(self, mock_get):
        mock_get.return_value = _mock_response()
        # RATE_LIMIT_PER_MINUTE default is 30; hammer it well past that
        # using distinct URLs so cache doesn't short-circuit the calls.
        last_status = None
        for i in range(35):
            resp = client.post("/audit", json={"url": f"https://example.com/{i}"})
            last_status = resp.status_code
        assert last_status == 429


class TestRequestIdHeader:
    def test_response_has_request_id_header(self):
        resp = client.get("/health")
        assert "X-Request-ID" in resp.headers
