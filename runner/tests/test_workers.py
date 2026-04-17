# test_workers.py — tests for parallel worker claiming and job isolation
import threading

from runbook import _claim_next_job


class TestClaimNextJob:
    def test_returns_none_when_queue_empty(self, tmp_path):
        queue_dir  = tmp_path / "queue"
        active_dir = tmp_path / "active"
        queue_dir.mkdir()

        result = _claim_next_job(queue_dir, active_dir)
        assert result is None

    def test_claims_and_moves_file_to_active(self, tmp_path):
        queue_dir  = tmp_path / "queue"
        active_dir = tmp_path / "active"
        queue_dir.mkdir()
        job = queue_dir / "job-001.md"
        job.write_text("---\njob_id: job-001\n---\ntest")

        claimed = _claim_next_job(queue_dir, active_dir)

        assert claimed is not None
        assert claimed.parent == active_dir
        assert claimed.name == "job-001.md"
        assert not job.exists()

    def test_claims_in_sorted_order(self, tmp_path):
        queue_dir  = tmp_path / "queue"
        active_dir = tmp_path / "active"
        queue_dir.mkdir()
        (queue_dir / "job-002.md").write_text("b")
        (queue_dir / "job-001.md").write_text("a")

        claimed = _claim_next_job(queue_dir, active_dir)
        assert claimed.name == "job-001.md"

    def test_two_workers_claim_different_jobs(self, tmp_path):
        """Concurrent workers must not both claim the same job."""
        queue_dir  = tmp_path / "queue"
        active_dir = tmp_path / "active"
        queue_dir.mkdir()

        # Two jobs, two workers — each should get exactly one
        (queue_dir / "job-001.md").write_text("a")
        (queue_dir / "job-002.md").write_text("b")

        claimed = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()  # both threads start simultaneously
            result = _claim_next_job(queue_dir, active_dir)
            if result is not None:
                claimed.append(result.name)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(claimed) == 2
        assert set(claimed) == {"job-001.md", "job-002.md"}

    def test_one_job_two_workers_no_double_claim(self, tmp_path):
        """Only one worker should claim a job when both race for the same file."""
        queue_dir  = tmp_path / "queue"
        active_dir = tmp_path / "active"
        queue_dir.mkdir()
        (queue_dir / "job-001.md").write_text("a")

        claimed = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            result = _claim_next_job(queue_dir, active_dir)
            if result is not None:
                claimed.append(result.name)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one worker wins; no double-claim
        assert len(claimed) == 1
        assert claimed[0] == "job-001.md"
