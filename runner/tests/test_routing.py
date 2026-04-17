# test_routing.py — tests for multi-machine Ollama routing
from unittest.mock import patch, MagicMock
from runbook import resolve_ollama_config, check_runner_health


class TestResolveOllamaConfig:
    def test_routes_gemma_to_davas(self, cfg):
        post = {"model": "gemma4-runbook:latest"}
        base_url, model = resolve_ollama_config(post, cfg)
        assert base_url == cfg["runners"]["davas"]["base_url"]

    def test_routes_qwen14b_to_contabo(self, cfg):
        post = {"model": "qwen2.5:14b"}
        base_url, model = resolve_ollama_config(post, cfg)
        assert base_url == cfg["runners"]["contabo"]["base_url"]

    def test_routes_qwen7b_to_contabo(self, cfg):
        post = {"model": "qwen2.5:7b"}
        base_url, model = resolve_ollama_config(post, cfg)
        assert base_url == cfg["runners"]["contabo"]["base_url"]

    def test_falls_back_to_default_for_unknown_model(self, cfg):
        post = {"model": "unknown-model:latest"}
        base_url, model = resolve_ollama_config(post, cfg)
        assert base_url == cfg["ollama"]["base_url"]

    def test_returns_runner_default_model_when_no_model_in_post(self, cfg):
        # Job with no model specified should fall back cleanly
        post = {}
        base_url, model = resolve_ollama_config(post, cfg)
        assert base_url == cfg["ollama"]["base_url"]

    def test_returns_tuple(self, cfg):
        post = {"model": "qwen2.5:14b"}
        result = resolve_ollama_config(post, cfg)
        assert isinstance(result, tuple)
        assert len(result) == 2


class TestCheckRunnerHealth:
    def test_returns_true_on_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("runbook.requests.get", return_value=mock_resp):
            assert check_runner_health("http://example.com:11434") is True

    def test_returns_false_on_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 530
        with patch("runbook.requests.get", return_value=mock_resp):
            assert check_runner_health("http://example.com:11434") is False

    def test_returns_false_on_connection_error(self):
        with patch("runbook.requests.get", side_effect=Exception("timeout")):
            assert check_runner_health("http://unreachable:11434") is False

    def test_uses_provided_timeout(self):
        with patch("runbook.requests.get", side_effect=Exception("timeout")) as mock_get:
            check_runner_health("http://example.com:11434", timeout=3)
            mock_get.assert_called_once_with("http://example.com:11434/api/tags", timeout=3)
