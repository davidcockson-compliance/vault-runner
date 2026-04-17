# test_memory.py — tests for MemoryManager (MemPalace integration)
#
# MemoryManager uses a lazy chromadb import inside _get_collection(), so chromadb
# does not need to be installed for the runner to start. Tests mock the chromadb
# module via sys.modules to avoid requiring a real palace on disk.
import sys
from unittest.mock import MagicMock, patch

from runbook import MemoryManager


def _mock_chroma(docs=None, metas=None, dists=None):
    """Return (mock_client, mock_collection) with canned query results."""
    mock_collection = MagicMock()
    mock_client     = MagicMock()
    mock_client.get_collection.return_value = mock_collection
    mock_collection.query.return_value = {
        "documents": [docs  or []],
        "metadatas": [metas or []],
        "distances": [dists or []],
    }
    return mock_client, mock_collection


def _chroma_patch(mock_client):
    """Context manager that stubs chromadb.PersistentClient."""
    mock_chromadb = MagicMock()
    mock_chromadb.PersistentClient.return_value = mock_client
    return patch.dict(sys.modules, {"chromadb": mock_chromadb})


def _miner_patch():
    """Context manager that stubs mempalace.miner."""
    miner_mod = MagicMock()
    return patch.dict(sys.modules, {"mempalace": MagicMock(), "mempalace.miner": miner_mod}), miner_mod


class TestMemoryManagerDisabled:
    """When store_path is empty, all methods are no-ops — no chromadb calls."""

    def setup_method(self):
        self.memory = MemoryManager(store_path="", logger=None)

    def test_disabled_when_store_path_empty(self):
        assert not self.memory.enabled

    def test_index_is_noop(self, tmp_path):
        f = tmp_path / "output.md"
        f.write_text("content")
        self.memory.index(f)  # must not raise, must not import chromadb

    def test_search_returns_empty_string(self):
        assert self.memory.search("anything") == ""

    def test_count_returns_zero(self):
        assert self.memory.count() == 0


class TestMemoryManagerCollectionCaching:
    """_get_collection() creates the client once and reuses it across calls."""

    def test_client_created_once_across_multiple_searches(self):
        mock_client, _ = _mock_chroma()
        memory = MemoryManager(store_path="/fake/store", logger=None)
        with _chroma_patch(mock_client):
            memory.search("query one")
            memory.search("query two")
            memory.search("query three")
            # PersistentClient instantiated exactly once
            assert sys.modules["chromadb"].PersistentClient.call_count == 1

    def test_client_created_once_across_search_and_count(self):
        mock_client, mock_col = _mock_chroma()
        mock_col.count.return_value = 5
        memory = MemoryManager(store_path="/fake/store", logger=None)
        with _chroma_patch(mock_client):
            memory.search("query")
            memory.count()
            assert sys.modules["chromadb"].PersistentClient.call_count == 1


class TestMemoryManagerIndex:
    """index() delegates to mempalace.miner.process_file with cached collection."""

    def setup_method(self):
        self.logger = MagicMock()
        self.memory = MemoryManager(store_path="/fake/store", logger=self.logger)

    def test_index_calls_process_file(self, tmp_path):
        output_file = tmp_path / "job-123-output.md"
        output_file.write_text("---\njob_id: job-123\n---\nSome output.")

        mock_client, mock_collection = _mock_chroma()
        miner_mod = MagicMock()
        with _chroma_patch(mock_client), \
             patch.dict(sys.modules, {"mempalace": MagicMock(), "mempalace.miner": miner_mod}):
            self.memory.index(output_file)

        miner_mod.process_file.assert_called_once_with(
            filepath=output_file,
            project_path=output_file.parent,
            collection=mock_collection,
            wing="runner-outputs",
            rooms=[{"name": "output", "description": "Runner job outputs"}],
            agent="runner",
            dry_run=False,
        )

    def test_index_swallows_exceptions(self, tmp_path):
        output_file = tmp_path / "job-err.md"
        output_file.write_text("content")

        mock_client = MagicMock()
        mock_client.get_collection.side_effect = RuntimeError("db error")
        miner_mod = MagicMock()
        with _chroma_patch(mock_client), \
             patch.dict(sys.modules, {"mempalace": MagicMock(), "mempalace.miner": miner_mod}):
            self.memory.index(output_file)  # must not raise

        self.logger.emit.assert_called_once()
        assert self.logger.emit.call_args[0][0] == "memory_index_error"

    def test_index_custom_wing(self, tmp_path):
        output_file = tmp_path / "book-output.md"
        output_file.write_text("content")

        mock_client, _ = _mock_chroma()
        miner_mod = MagicMock()
        with _chroma_patch(mock_client), \
             patch.dict(sys.modules, {"mempalace": MagicMock(), "mempalace.miner": miner_mod}):
            self.memory.index(output_file, wing="books")

        assert miner_mod.process_file.call_args.kwargs["wing"] == "books"


class TestMemoryManagerSearch:
    """search() queries ChromaDB directly and formats results as a context block."""

    def setup_method(self):
        self.logger = MagicMock()
        self.memory = MemoryManager(store_path="/fake/store", logger=self.logger)

    def test_search_no_results_returns_empty_string(self):
        mock_client, _ = _mock_chroma()
        with _chroma_patch(mock_client):
            assert self.memory.search("anything") == ""

    def test_search_formats_results_as_context_block(self):
        docs  = ["Prior knowledge A", "Prior knowledge B"]
        metas = [
            {"source_file": "/some/path/job-01-output.md"},
            {"source_file": "/some/path/job-02-output.md"},
        ]
        dists = [0.08, 0.22]
        mock_client, _ = _mock_chroma(docs=docs, metas=metas, dists=dists)

        with _chroma_patch(mock_client):
            result = self.memory.search("test query", n=2)

        assert result.startswith("--- Relevant prior context ---")
        assert "Prior knowledge A" in result
        assert "Prior knowledge B" in result
        assert "job-01-output.md" in result
        assert "0.92" in result  # 1 - 0.08
        assert result.endswith("--- End prior context ---\n\n")

    def test_search_passes_correct_args_to_collection(self):
        mock_client, mock_collection = _mock_chroma()
        with _chroma_patch(mock_client):
            self.memory.search("my query", n=5)

        mock_collection.query.assert_called_once_with(
            query_texts=["my query"],
            n_results=5,
            include=["documents", "metadatas", "distances"],
        )

    def test_search_swallows_exceptions(self):
        mock_client = MagicMock()
        mock_client.get_collection.side_effect = RuntimeError("chroma error")
        with _chroma_patch(mock_client):
            result = self.memory.search("query")  # must not raise

        assert result == ""
        self.logger.emit.assert_called_once()
        assert self.logger.emit.call_args[0][0] == "memory_search_error"


class TestMemoryManagerSmartSearch:
    """smart_search() generates queries via LLM then searches MemPalace."""

    def setup_method(self):
        self.logger = MagicMock()
        self.memory = MemoryManager(store_path="/fake/store", logger=self.logger)
        self.cfg = {
            "ollama": {"base_url": "http://localhost:11434", "default_model": "qwen2.5:14b"},
            "mempalace": {"smart_query_model": "qwen2.5:7b", "pre_job_results": 2},
        }

    def test_smart_search_runs_multiple_queries(self):
        docs  = ["SRE principles", "Reliability engineering"]
        metas = [{"source_file": "sre-book.md"}, {"source_file": "becoming-sre.md"}]
        dists = [0.1, 0.2]
        mock_client, mock_collection = _mock_chroma(docs=docs, metas=metas, dists=dists)

        with _chroma_patch(mock_client), \
             patch("runbook.call_ollama") as mock_call, \
             patch("runbook._extract_json_array") as mock_extract:
            mock_call.return_value = {"response": '["SRE career", "site reliability"]'}
            mock_extract.return_value = ["SRE career", "site reliability"]
            result = self.memory.smart_search("How to become an SRE", cfg=self.cfg)

        assert "--- Relevant prior context (smart) ---" in result
        # collection.query called once per generated query
        assert mock_collection.query.call_count == 2

    def test_smart_search_deduplicates_by_source(self):
        # Both queries return the same source file
        docs  = ["SRE content"]
        metas = [{"source_file": "sre-book.md"}]
        dists = [0.1]
        mock_client, mock_collection = _mock_chroma(docs=docs, metas=metas, dists=dists)

        with _chroma_patch(mock_client), \
             patch("runbook.call_ollama") as mock_call, \
             patch("runbook._extract_json_array") as mock_extract:
            mock_call.return_value = {"response": "[]"}
            mock_extract.return_value = ["query one", "query two"]
            result = self.memory.smart_search("task", cfg=self.cfg)

        # Only one hit even though two queries both returned the same source
        assert result.count("sre-book.md") == 1

    def test_smart_search_falls_back_on_llm_failure(self):
        docs  = ["fallback result"]
        metas = [{"source_file": "fallback.md"}]
        dists = [0.15]
        mock_client, _ = _mock_chroma(docs=docs, metas=metas, dists=dists)

        with _chroma_patch(mock_client), \
             patch("runbook.call_ollama") as mock_call:
            mock_call.side_effect = RuntimeError("ollama timeout")
            result = self.memory.smart_search("task", cfg=self.cfg)

        # Falls back to basic search — returns context block
        assert "--- Relevant prior context ---" in result
        self.logger.emit.assert_called_with(
            "smart_memory_query_failed", error="ollama timeout"
        )

    def test_smart_search_disabled_returns_empty(self):
        memory = MemoryManager(store_path="", logger=None)
        assert memory.smart_search("task", cfg=self.cfg) == ""


class TestMemoryManagerCount:
    def setup_method(self):
        self.memory = MemoryManager(store_path="/fake/store", logger=None)

    def test_count_returns_collection_count(self):
        mock_client, mock_col = _mock_chroma()
        mock_col.count.return_value = 42
        with _chroma_patch(mock_client):
            assert self.memory.count() == 42

    def test_count_returns_minus_one_on_error(self):
        mock_client = MagicMock()
        mock_client.get_collection.side_effect = RuntimeError("fail")
        with _chroma_patch(mock_client):
            assert self.memory.count() == -1
