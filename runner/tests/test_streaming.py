# test_streaming.py — tests for streaming Ollama calls and SSE endpoint
import json
from unittest.mock import MagicMock, patch

import pytest

import runbook
from runbook import (
    JobCancelledError,  # noqa: F401 — used in pytest.raises()
    _cleanup_stream,
    call_ollama_streaming,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ndjson_lines(*chunks, eval_count=42):
    """Build a list of NDJSON byte-lines as Ollama streaming would return them."""
    lines = [
        json.dumps({"response": c, "done": False, "model": "test"}).encode()
        for c in chunks
    ]
    lines.append(
        json.dumps({"response": "", "done": True, "eval_count": eval_count, "model": "test"}).encode()
    )
    return lines


def _mock_streaming_response(lines):
    """Return a mock requests.Response whose iter_lines() yields the given lines."""
    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = iter(lines)
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


# ─── call_ollama_streaming ─────────────────────────────────────────────────────

class TestCallOllamaStreaming:
    def test_writes_chunks_to_file(self, tmp_path):
        stream_path = tmp_path / "job-001.txt"
        lines = _ndjson_lines("Hello", ", ", "world", "!")

        with patch("runbook.requests.post") as mock_post:
            mock_post.return_value.__enter__ = lambda s: _mock_streaming_response(lines)
            mock_post.return_value.__exit__ = MagicMock(return_value=False)
            mock_post.return_value = _mock_streaming_response(lines)

            result = call_ollama_streaming(
                base_url="http://localhost:11434",
                model="test",
                prompt="hi",
                stream_path=stream_path,
            )

        assert stream_path.exists()
        assert stream_path.read_text() == "Hello, world!"
        assert result["response"] == "Hello, world!"
        assert result["eval_count"] == 42

    def test_done_sidecar_created(self, tmp_path):
        stream_path = tmp_path / "job-002.txt"
        lines = _ndjson_lines("ok")

        with patch("runbook.requests.post", return_value=_mock_streaming_response(lines)):
            call_ollama_streaming(
                base_url="http://localhost:11434",
                model="test",
                prompt="hi",
                stream_path=stream_path,
            )

        assert stream_path.with_suffix(".done").exists()
        assert not stream_path.with_suffix(".error").exists()
        assert not stream_path.with_suffix(".cancelled").exists()

    def test_error_sidecar_on_failure(self, tmp_path):
        stream_path = tmp_path / "job-003.txt"

        with patch("runbook.requests.post", side_effect=ConnectionError("refused")):
            with pytest.raises(ConnectionError):
                call_ollama_streaming(
                    base_url="http://localhost:11434",
                    model="test",
                    prompt="hi",
                    stream_path=stream_path,
                )

        assert stream_path.with_suffix(".error").exists()
        assert "refused" in stream_path.with_suffix(".error").read_text()

    def test_cancelled_sidecar_on_cancellation(self, tmp_path):
        stream_path = tmp_path / "job-004.txt"
        lines = _ndjson_lines("chunk1", "chunk2")

        call_count = 0

        def cancel_fn():
            nonlocal call_count
            call_count += 1
            return call_count >= 2  # cancel after first chunk

        with patch("runbook.requests.post", return_value=_mock_streaming_response(lines)):
            with pytest.raises(JobCancelledError):
                call_ollama_streaming(
                    base_url="http://localhost:11434",
                    model="test",
                    prompt="hi",
                    stream_path=stream_path,
                    cancel_fn=cancel_fn,
                )

        assert stream_path.with_suffix(".cancelled").exists()

    def test_stale_signals_cleared_on_retry(self, tmp_path):
        stream_path = tmp_path / "job-005.txt"
        # Simulate stale signals from a previous attempt
        stream_path.with_suffix(".error").write_text("old error")
        stream_path.with_suffix(".done").touch()

        lines = _ndjson_lines("fresh")
        with patch("runbook.requests.post", return_value=_mock_streaming_response(lines)):
            result = call_ollama_streaming(
                base_url="http://localhost:11434",
                model="test",
                prompt="hi",
                stream_path=stream_path,
            )

        assert not stream_path.with_suffix(".error").exists()
        assert stream_path.with_suffix(".done").exists()
        assert result["response"] == "fresh"

    def test_vision_payload_includes_image(self, tmp_path):
        stream_path = tmp_path / "job-006.txt"
        image_path = tmp_path / "test.png"
        image_path.write_bytes(b"\x89PNG\r\n")

        lines = _ndjson_lines("described")
        captured = {}

        def fake_post(url, json=None, stream=False, timeout=None):
            captured["payload"] = json
            return _mock_streaming_response(lines)

        with patch("runbook.requests.post", side_effect=fake_post):
            call_ollama_streaming(
                base_url="http://localhost:11434",
                model="test",
                prompt="describe",
                stream_path=stream_path,
                image_path=image_path,
            )

        assert "images" in captured["payload"]
        assert len(captured["payload"]["images"]) == 1
        assert captured["payload"]["stream"] is True


# ─── _cleanup_stream ──────────────────────────────────────────────────────────

class TestCleanupStream:
    def test_removes_all_sidecar_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runbook, "_STREAMS_DIR", tmp_path)

        job_id = "job-clean-001"
        for suffix in (".txt", ".done", ".error", ".cancelled"):
            (tmp_path / f"{job_id}{suffix}").touch()

        _cleanup_stream.__globals__["_STREAMS_DIR"] = tmp_path
        # Patch the module-level constant for this call
        with patch.object(runbook, "_STREAMS_DIR", tmp_path):
            _cleanup_stream(job_id)

        for suffix in (".txt", ".done", ".error", ".cancelled"):
            assert not (tmp_path / f"{job_id}{suffix}").exists()

    def test_no_error_if_files_missing(self, tmp_path):
        with patch.object(runbook, "_STREAMS_DIR", tmp_path):
            _cleanup_stream("nonexistent-job")  # should not raise
