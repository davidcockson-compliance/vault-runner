# test_searxng.py — unit tests for call_searxng()
from unittest.mock import MagicMock, patch

import pytest
import requests

from runbook import call_searxng


@pytest.fixture
def searxng_cfg():
    return {
        "searxng": {
            "base_url": "http://search.homelab.local",
            "default_categories": "it",
            "num_results": 3,
            "timeout": 10,
            "enabled": True,
        }
    }


def _mock_response(results):
    resp = MagicMock()
    resp.json.return_value = {"query": "test", "results": results}
    resp.raise_for_status.return_value = None
    return resp


class TestCallSearxng:
    def test_returns_formatted_results(self, searxng_cfg):
        results = [
            {"title": "Async Python", "url": "https://example.com/1", "content": "Good stuff.", "score": 9.0},
            {"title": "Asyncio Guide", "url": "https://example.com/2", "content": "More detail.", "score": 8.5},
        ]
        with patch("runbook.requests.get", return_value=_mock_response(results)):
            output = call_searxng("python asyncio", searxng_cfg)

        assert "## Search Results: python asyncio" in output
        assert "Async Python" in output
        assert "https://example.com/1" in output
        assert "Good stuff." in output
        assert "Asyncio Guide" in output

    def test_passes_default_categories(self, searxng_cfg):
        with patch("runbook.requests.get", return_value=_mock_response([])) as mock_get:
            call_searxng("docker", searxng_cfg)

        _, kwargs = mock_get.call_args
        assert mock_get.call_args[1]["params"]["categories"] == "it"

    def test_overrides_categories(self, searxng_cfg):
        with patch("runbook.requests.get", return_value=_mock_response([])) as mock_get:
            call_searxng("kubernetes", searxng_cfg, categories="general")

        params = mock_get.call_args[1]["params"]
        assert params["categories"] == "general"

    def test_passes_engines_when_specified(self, searxng_cfg):
        with patch("runbook.requests.get", return_value=_mock_response([])) as mock_get:
            call_searxng("flask", searxng_cfg, engines="github,stackoverflow")

        params = mock_get.call_args[1]["params"]
        assert params["engines"] == "github,stackoverflow"

    def test_no_engines_param_when_not_specified(self, searxng_cfg):
        with patch("runbook.requests.get", return_value=_mock_response([])) as mock_get:
            call_searxng("flask", searxng_cfg)

        params = mock_get.call_args[1]["params"]
        assert "engines" not in params

    def test_returns_placeholder_when_no_results(self, searxng_cfg):
        with patch("runbook.requests.get", return_value=_mock_response([])):
            output = call_searxng("xyzunknownquery", searxng_cfg)

        assert "No results found for: xyzunknownquery" in output

    def test_respects_num_results_limit(self, searxng_cfg):
        results = [
            {"title": f"Result {i}", "url": f"https://example.com/{i}", "content": f"Content {i}"}
            for i in range(10)
        ]
        with patch("runbook.requests.get", return_value=_mock_response(results)):
            output = call_searxng("many results", searxng_cfg)

        # num_results is 3 in fixture — only first 3 should appear
        assert "Result 0" in output
        assert "Result 2" in output
        assert "Result 3" not in output

    def test_raises_on_http_error(self, searxng_cfg):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("503 Service Unavailable")
        with patch("runbook.requests.get", return_value=resp):
            with pytest.raises(requests.HTTPError):
                call_searxng("test", searxng_cfg)

    def test_raises_on_connection_error(self, searxng_cfg):
        with patch("runbook.requests.get", side_effect=requests.ConnectionError("unreachable")):
            with pytest.raises(requests.ConnectionError):
                call_searxng("test", searxng_cfg)

    def test_uses_base_url_from_config(self, searxng_cfg):
        searxng_cfg["searxng"]["base_url"] = "http://10.0.0.5:8080"
        with patch("runbook.requests.get", return_value=_mock_response([])) as mock_get:
            call_searxng("test", searxng_cfg)

        url = mock_get.call_args[0][0]
        assert url == "http://10.0.0.5:8080/search"

    def test_handles_missing_result_fields_gracefully(self, searxng_cfg):
        # Results with only partial fields — should not raise
        results = [{"score": 5.0}]
        with patch("runbook.requests.get", return_value=_mock_response(results)):
            output = call_searxng("sparse", searxng_cfg)

        assert "No title" in output
