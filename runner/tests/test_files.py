# test_files.py — tests for job file movement and startup recovery
from runbook import move_job, _recover_active


class TestMoveJob:
    def test_moves_file_to_destination(self, tmp_path):
        src_dir = tmp_path / "queue"
        dst_dir = tmp_path / "active"
        src_dir.mkdir()
        dst_dir.mkdir()

        job = src_dir / "test-job.md"
        job.write_text("---\njob_id: test\n---\nBody here.")

        result = move_job(job, dst_dir)

        assert not job.exists()
        assert result.exists()
        assert result.parent == dst_dir

    def test_returns_new_path(self, tmp_path):
        src_dir = tmp_path / "queue"
        dst_dir = tmp_path / "active"
        src_dir.mkdir()
        dst_dir.mkdir()

        job = src_dir / "my-job.md"
        job.write_text("---\njob_id: abc\n---\n")

        result = move_job(job, dst_dir)

        assert result == dst_dir / "my-job.md"

    def test_preserves_file_content(self, tmp_path):
        src_dir = tmp_path / "queue"
        dst_dir = tmp_path / "active"
        src_dir.mkdir()
        dst_dir.mkdir()

        content = "---\njob_id: preserve-test\n---\nImportant content."
        job = src_dir / "preserve.md"
        job.write_text(content)

        result = move_job(job, dst_dir)

        assert result.read_text() == content

    def test_creates_destination_if_missing(self, tmp_path):
        src_dir = tmp_path / "queue"
        src_dir.mkdir()
        dst_dir = tmp_path / "new-destination"
        # dst_dir intentionally not created

        job = src_dir / "test-job.md"
        job.write_text("---\njob_id: test\n---\n")

        result = move_job(job, dst_dir)

        assert result.exists()


class TestRecoverActive:
    def test_moves_stuck_file_back_to_queue(self, tmp_path, mock_logger):
        active = tmp_path / "_active"
        queue = tmp_path / "_queue"
        active.mkdir()
        queue.mkdir()

        stale = active / "job-stuck.md"
        stale.write_text("---\njob_id: stuck\n---\n")

        cfg = {
            "dirs": {
                "active": str(active),
                "queue": str(queue),
            }
        }

        _recover_active(cfg, mock_logger)

        assert not stale.exists()
        assert (queue / "job-stuck.md").exists()

    def test_empty_active_dir_does_nothing(self, tmp_path, mock_logger):
        active = tmp_path / "_active"
        queue = tmp_path / "_queue"
        active.mkdir()
        queue.mkdir()

        cfg = {
            "dirs": {
                "active": str(active),
                "queue": str(queue),
            }
        }

        _recover_active(cfg, mock_logger)

        assert list(queue.iterdir()) == []

    def test_recovers_multiple_stuck_files(self, tmp_path, mock_logger):
        active = tmp_path / "_active"
        queue = tmp_path / "_queue"
        active.mkdir()
        queue.mkdir()

        for name in ["job-a.md", "job-b.md", "job-c.md"]:
            (active / name).write_text(f"---\njob_id: {name}\n---\n")

        cfg = {
            "dirs": {
                "active": str(active),
                "queue": str(queue),
            }
        }

        _recover_active(cfg, mock_logger)

        assert list(active.iterdir()) == []
        assert len(list(queue.iterdir())) == 3
