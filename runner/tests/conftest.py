# conftest.py — shared pytest fixtures for the runner test suite
import pytest
from unittest.mock import MagicMock


@pytest.fixture
def cfg():
    """Minimal config dict mirroring the real config.yaml structure."""
    return {
        "ollama": {
            "base_url": "http://localhost:11434",
            "default_model": "qwen2.5:14b",
            "timeout": 600,
            "chain_timeout": 900,
        },
        "runners": {
            "contabo": {
                "base_url": "http://localhost:11434",
                "default_model": "qwen2.5:14b",
            },
            "davas": {
                "base_url": "http://10.0.0.2:11434",
                "default_model": "gemma4-runbook:latest",
            },
        },
        "model_runners": {
            "gemma4-runbook:latest": "davas",
            "qwen2.5:14b": "contabo",
            "qwen2.5:7b": "contabo",
        },
        "dirs": {
            "queue":     "/tmp/queue",
            "active":    "/tmp/active",
            "output":    "/tmp/output",
            "completed": "/tmp/completed",
            "failed":    "/tmp/failed",
        },
    }


@pytest.fixture
def mock_logger():
    """Fake RunnerLogger — captures emit() calls without touching the filesystem."""
    return MagicMock()
