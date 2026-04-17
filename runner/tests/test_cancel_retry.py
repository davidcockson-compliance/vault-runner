# test_cancel_retry.py — tests for job cancellation and retry logic
import threading
from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import pytest

import runbook
from runbook import (
    CancellationRegistry,
    cancel_registry,
    process_job,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _write_job(directory: Path, job_id: str, extra_frontmatter: str = "") -> Path:
    """Write a minimal valid job file into the given directory."""
    directory.mkdir(parents=True, exist_ok=True)
    content = f"---\njob_id: {job_id}\ntype: text\nmodel: qwen2.5:14b\n{extra_frontmatter}---\n\nDo something.\n"
    path = directory / f"{job_id}.md"
    path.write_text(content)
    return path


@pytest.fixture(autouse=True)
def reset_cancel_registry():
    """Clear the module-level singleton before each test to prevent state leakage."""
    cancel_registry._cancelled.clear()
    yield
    cancel_registry._cancelled.clear()


@pytest.fixture
def dirs(tmp_path):
    """tmp_path-based dirs dict for process_job tests."""
    d = {
        "queue":     str(tmp_path / "_queue"),
        "active":    str(tmp_path / "_active"),
        "output":    str(tmp_path / "_output"),
        "completed": str(tmp_path / "_completed"),
        "failed":    str(tmp_path / "_failed"),
    }
    for v in d.values():
        Path(v).mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def job_cfg(dirs):
    """Full cfg dict wired to tmp_path dirs."""
    return {
        "vault_path": str(Path(dirs["queue"]).parent),
        "dirs": dirs,
        "ollama": {
            "base_url": "http://localhost:11434",
            "default_model": "qwen2.5:14b",
            "timeout": 60,
            "chain_timeout": 120,
        },
        "runners": {},
        "model_runners": {},
        "mempalace": {"enabled": False},
    }


@pytest.fixture
def mock_tracer():
    tracer = MagicMock()
    span = MagicMock()
    span.__enter__ = MagicMock(return_value=span)
    span.__exit__ = MagicMock(return_value=False)
    tracer.start_as_current_span.return_value = span
    return tracer


@pytest.fixture
def mock_memory():
    return MagicMock()


# ─── CancellationRegistry unit tests ──────────────────────────────────────────

class TestCancellationRegistry:
    def test_request_and_is_requested(self):
        reg = CancellationRegistry()
        reg.request("job-abc")
        assert reg.is_requested("job-abc") is True

    def test_is_requested_false_when_not_set(self):
        reg = CancellationRegistry()
        assert reg.is_requested("job-xyz") is False

    def test_consume_removes_entry_and_returns_true(self):
        reg = CancellationRegistry()
        reg.request("job-abc")
        assert reg.consume("job-abc") is True
        assert reg.is_requested("job-abc") is False

    def test_consume_returns_false_if_not_set(self):
        reg = CancellationRegistry()
        assert reg.consume("job-none") is False

    def test_thread_safety_concurrent_requests(self):
        """Many threads calling request() concurrently must not corrupt the set."""
        reg = CancellationRegistry()
        job_ids = [f"job-{i:04d}" for i in range(100)]

        def register(job_id):
            reg.request(job_id)

        threads = [threading.Thread(target=register, args=(jid,)) for jid in job_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(reg.is_requested(jid) for jid in job_ids)


# ─── process_job cancellation tests ───────────────────────────────────────────

class TestProcessJobCancellation:
    def test_pre_start_cancel_moves_to_failed(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        """A job cancelled before process_job starts should move straight to _failed/."""
        active_dir = Path(job_cfg["dirs"]["active"])
        failed_dir = Path(job_cfg["dirs"]["failed"])
        job_file = _write_job(active_dir, "job-cancel-prestart")

        cancel_registry.request("job-cancel-prestart")

        monkeypatch.setattr(runbook, "call_ollama", MagicMock(side_effect=AssertionError("should not be called")))
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        assert not job_file.exists()
        assert (failed_dir / "job-cancel-prestart.md").exists()

    def test_pre_start_cancel_does_not_call_ollama(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        active_dir = Path(job_cfg["dirs"]["active"])
        job_file = _write_job(active_dir, "job-cancel-no-ollama")
        cancel_registry.request("job-cancel-no-ollama")

        ollama_mock = MagicMock()
        monkeypatch.setattr(runbook, "call_ollama", ollama_mock)
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        ollama_mock.assert_not_called()

    def test_cancelled_job_not_retried(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        """A cancelled job with retries: 2 must still go to _failed/, not back to queue."""
        active_dir = Path(job_cfg["dirs"]["active"])
        failed_dir = Path(job_cfg["dirs"]["failed"])
        queue_dir  = Path(job_cfg["dirs"]["queue"])
        job_file = _write_job(active_dir, "job-cancel-no-retry", "retries: 2\n")

        cancel_registry.request("job-cancel-no-retry")
        monkeypatch.setattr(runbook, "call_ollama", MagicMock())
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        assert (failed_dir / "job-cancel-no-retry.md").exists()
        assert not list(queue_dir.glob("*.md")), "cancelled job must not be requeued"


# ─── Retry logic tests ────────────────────────────────────────────────────────

class TestRetryLogic:
    def _make_failing_ollama(self):
        return MagicMock(side_effect=ConnectionError("ollama unreachable"))

    def test_job_requeued_on_failure_when_retries_set(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        """retries: 1 → first failure requeues the job."""
        active_dir = Path(job_cfg["dirs"]["active"])
        queue_dir  = Path(job_cfg["dirs"]["queue"])
        job_file = _write_job(active_dir, "job-retry-1", "retries: 1\n")

        monkeypatch.setattr(runbook, "call_ollama", self._make_failing_ollama())
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        requeued = list(queue_dir.glob("*.md"))
        assert len(requeued) == 1, "job should be requeued"
        assert requeued[0].name == "job-retry-1.md"

    def test_retries_remaining_decremented(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        active_dir = Path(job_cfg["dirs"]["active"])
        queue_dir  = Path(job_cfg["dirs"]["queue"])
        job_file = _write_job(active_dir, "job-retry-decrement", "retries: 2\n")

        monkeypatch.setattr(runbook, "call_ollama", self._make_failing_ollama())
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        requeued = queue_dir / "job-retry-decrement.md"
        post = frontmatter.load(str(requeued))
        assert int(post["retries_remaining"]) == 1

    def test_last_error_written_to_frontmatter(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        active_dir = Path(job_cfg["dirs"]["active"])
        queue_dir  = Path(job_cfg["dirs"]["queue"])
        job_file = _write_job(active_dir, "job-retry-error-field", "retries: 1\n")

        monkeypatch.setattr(runbook, "call_ollama", self._make_failing_ollama())
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        post = frontmatter.load(str(queue_dir / "job-retry-error-field.md"))
        assert "last_error" in post.metadata

    def test_no_retries_goes_straight_to_failed(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        active_dir = Path(job_cfg["dirs"]["active"])
        failed_dir = Path(job_cfg["dirs"]["failed"])
        job_file = _write_job(active_dir, "job-no-retry")  # no retries field

        monkeypatch.setattr(runbook, "call_ollama", self._make_failing_ollama())
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        assert (failed_dir / "job-no-retry.md").exists()

    def test_retries_exhausted_lands_in_failed(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        """A job already on its last retry (retries_remaining: 0) goes to _failed/."""
        active_dir = Path(job_cfg["dirs"]["active"])
        failed_dir = Path(job_cfg["dirs"]["failed"])
        # Simulate a job that has already been retried once (retries_remaining: 0)
        job_file = _write_job(
            active_dir, "job-exhausted", "retries: 1\nretries_remaining: 0\n"
        )

        monkeypatch.setattr(runbook, "call_ollama", self._make_failing_ollama())
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        assert (failed_dir / "job-exhausted.md").exists()

    def test_successful_job_not_requeued(
        self, tmp_path, job_cfg, mock_tracer, mock_memory, monkeypatch
    ):
        """A job that succeeds should go to _completed/, not be requeued."""
        active_dir  = Path(job_cfg["dirs"]["active"])
        completed   = Path(job_cfg["dirs"]["completed"])
        queue_dir   = Path(job_cfg["dirs"]["queue"])
        job_file = _write_job(active_dir, "job-success", "retries: 3\n")

        monkeypatch.setattr(runbook, "call_ollama_streaming", MagicMock(return_value={"response": "ok", "eval_count": 10}))
        monkeypatch.setattr(runbook, "_cleanup_stream", MagicMock())
        monkeypatch.setattr(runbook, "notify_discord", MagicMock())

        process_job(job_file, job_cfg, MagicMock(), mock_tracer, mock_memory)

        assert (completed / "job-success.md").exists()
        assert not list(queue_dir.glob("*.md"))
