#!/usr/bin/env python3
"""
Obsidian LLM Runner — Phase 1 MVP
Polls _queue/ for job files (.md with YAML frontmatter), processes them
via Ollama, and moves files through: queue → active → output → completed/failed.
Emits structured JSON logs to RUNNER-LOG.md and OpenTelemetry traces to Tempo.
"""

import base64
import json
import logging
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import re

import requests
import yaml

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.resource import ResourceAttributes


# ─── Cancellation ─────────────────────────────────────────────────────────────

class JobCancelledError(Exception):
    """Raised when a cancellation signal is detected mid-job."""


class CancellationRegistry:
    """
    Thread-safe set of job_ids marked for cancellation.
    Web handlers call request(); worker threads call is_requested() / consume().
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()

    def request(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.add(job_id)

    def is_requested(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def consume(self, job_id: str) -> bool:
        """Remove and return True if job_id was pending cancellation."""
        with self._lock:
            if job_id in self._cancelled:
                self._cancelled.discard(job_id)
                return True
            return False


cancel_registry = CancellationRegistry()


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ─── Structured logging ───────────────────────────────────────────────────────

class RunnerLogger:
    """
    Dual-output logger:
    - Python logging → stdout (plain text, for journald / systemd)
    - Structured JSON entries → RUNNER-LOG.md (human-readable in Obsidian)
    """

    def __init__(self, log_file: Path):
        self.log_file = log_file
        self._file_lock = threading.Lock()  # prevents interleaved writes from parallel workers
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],
        )
        self._log = logging.getLogger("runner")

    def emit(self, event: str, job_id: str = None, **kwargs):
        """Write one structured JSON log entry to stdout and RUNNER-LOG.md."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": event,
            "job_id": job_id,
            **kwargs,
        }
        # Remove None values to keep log entries clean
        entry = {k: v for k, v in entry.items() if v is not None}

        pretty = json.dumps(entry, indent=2)
        self._log.info(pretty)

        # Append to RUNNER-LOG.md inside a fenced block so Obsidian renders it nicely
        with self._file_lock:
            with open(self.log_file, "a") as f:
                f.write(f"```json\n{pretty}\n```\n\n")


# ─── OpenTelemetry setup ──────────────────────────────────────────────────────

def setup_tracing(endpoint: str, service_name: str) -> trace.Tracer:
    """Configure OTel SDK to export traces to Tempo via OTLP gRPC."""
    resource = Resource.create({ResourceAttributes.SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


# ─── Memory (MemPalace) ───────────────────────────────────────────────────────

_MEMORY_QUERY_PROMPT = (
    "You are a search query assistant. Given a task description, generate 2-3 precise "
    "search queries to retrieve relevant background context from a knowledge base.\n\n"
    "Rules:\n"
    "- Output ONLY a valid JSON array of strings. No explanation, no preamble.\n"
    "- Each query targets a distinct aspect of the task.\n"
    "- Use specific noun phrases or concepts (3-8 words), not full sentences or questions.\n"
    "- Queries should match technical terms, book titles, frameworks, or named concepts.\n\n"
    "Task: {task}"
)


class MemoryManager:
    """
    Post-job indexer and pre-job context injector backed by MemPalace (ChromaDB).
    All errors are swallowed — memory failures must never block job processing.
    Pass store_path="" or enabled=False to disable without removing config.

    The ChromaDB PersistentClient is cached after first use — the HNSW backfill
    (expensive for large palaces) runs once per process, not once per search call.
    All ChromaDB access is serialised through _chroma_lock (sqlite3 is not thread-safe).
    """

    def __init__(self, store_path: str, logger, max_chars_per_result: int = 400):
        self.store_path = store_path
        self.enabled = bool(store_path)
        self._logger = logger
        self._max_chars = max_chars_per_result
        self._chroma_lock = threading.Lock()
        self._collection = None  # lazily initialised; HNSW backfill runs once per process

    def _emit(self, event: str, **kwargs) -> None:
        if self._logger:
            self._logger.emit(event, **kwargs)

    def _get_collection(self):
        """Return the cached ChromaDB collection. Must be called within _chroma_lock.
        Creates the PersistentClient on first call — HNSW backfill happens once."""
        if self._collection is None:
            import chromadb  # lazy import — runner starts even if chromadb is absent
            client = chromadb.PersistentClient(path=self.store_path)
            self._collection = client.get_collection("mempalace_drawers")
        return self._collection

    def index(self, file_path: Path, wing: str = "runner-outputs") -> None:
        """Index a single output file into the palace after a job completes."""
        if not self.enabled:
            return
        try:
            from mempalace.miner import process_file
            with self._chroma_lock:
                collection = self._get_collection()
                process_file(
                    filepath=file_path,
                    project_path=file_path.parent,
                    collection=collection,
                    wing=wing,
                    rooms=[{"name": "output", "description": "Runner job outputs"}],
                    agent="runner",
                    dry_run=False,
                )
        except Exception as exc:
            self._emit("memory_index_error", file=str(file_path), error=str(exc))

    def search(self, query: str, n: int = 3) -> str:
        """
        Search the palace and return a formatted context block, or "" if disabled/no results.
        Calls ChromaDB directly (bypasses mempalace.searcher) to reuse the cached collection.
        """
        if not self.enabled:
            return ""
        try:
            with self._chroma_lock:
                collection = self._get_collection()
                results = collection.query(
                    query_texts=[query],
                    n_results=n,
                    include=["documents", "metadatas", "distances"],
                )
            docs  = results["documents"][0]
            metas = results["metadatas"][0]
            dists = results["distances"][0]
            if not docs:
                return ""
            parts = [
                f"[{Path(meta.get('source_file', '?')).name} | similarity: {round(1 - dist, 3)}]\n"
                f"{doc[:self._max_chars]}"
                for doc, meta, dist in zip(docs, metas, dists)
            ]
            return (
                "--- Relevant prior context ---\n"
                + "\n---\n".join(parts)
                + "\n--- End prior context ---\n\n"
            )
        except Exception as exc:
            self._emit("memory_search_error", query=query[:80], error=str(exc))
            return ""

    def smart_search(self, task: str, cfg: dict, n_per_query: int = 2) -> str:
        """
        Generate targeted search queries via a fast LLM, then search MemPalace.
        Produces more precise context than dumb injection — the LLM decides what it needs.
        Falls back to basic search if query generation fails.
        """
        if not self.enabled:
            return ""

        # Step 1: generate search queries with the fast local model
        query_model = cfg.get("mempalace", {}).get("smart_query_model", "qwen2.5:7b")
        base_url    = cfg["ollama"]["base_url"]
        try:
            result  = call_ollama(
                base_url=base_url,
                model=query_model,
                prompt=_MEMORY_QUERY_PROMPT.format(task=task[:500]),
                timeout=30,
                retries=1,
                retry_delay=5,
            )
            queries = _extract_json_array(result.get("response", ""))[:3]
        except Exception as exc:
            self._emit("smart_memory_query_failed", error=str(exc))
            return self.search(task, n=n_per_query)  # graceful fallback

        # Step 2: search each query, deduplicate by source file
        all_hits: list[tuple] = []
        seen_sources: set[str] = set()
        try:
            with self._chroma_lock:
                collection = self._get_collection()
                for query in queries:
                    try:
                        r = collection.query(
                            query_texts=[query],
                            n_results=n_per_query,
                            include=["documents", "metadatas", "distances"],
                        )
                        for doc, meta, dist in zip(
                            r["documents"][0], r["metadatas"][0], r["distances"][0]
                        ):
                            src = meta.get("source_file", "")
                            if src not in seen_sources:
                                seen_sources.add(src)
                                all_hits.append((doc, meta, dist))
                    except Exception:
                        continue  # one failed query doesn't abort the others
        except Exception as exc:
            self._emit("smart_memory_search_failed", error=str(exc))
            return ""

        if not all_hits:
            return ""

        all_hits.sort(key=lambda x: x[2])  # best similarity first
        parts = [
            f"[{Path(meta.get('source_file', '?')).name} | similarity: {round(1 - dist, 3)}]\n"
            f"{doc[:self._max_chars]}"
            for doc, meta, dist in all_hits
        ]
        return (
            "--- Relevant prior context (smart) ---\n"
            + "\n---\n".join(parts)
            + "\n--- End prior context ---\n\n"
        )

    def count(self) -> int:
        """Return total indexed documents, or -1 on error."""
        if not self.enabled:
            return 0
        try:
            with self._chroma_lock:
                return self._get_collection().count()
        except Exception:
            return -1


# ─── File state machine ───────────────────────────────────────────────────────

def move_job(src: Path, dst_dir: Path) -> Path:
    """Move a job file to dst_dir, creating it if needed. Returns the new path."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    return dst


# ─── Ollama API ───────────────────────────────────────────────────────────────

def call_ollama(
    base_url: str,
    model: str,
    prompt: str,
    image_path: Path = None,
    timeout: int = 300,
    retries: int = 3,
    retry_delay: int = 15,
) -> dict:
    """
    POST to Ollama /api/generate. Supports text and vision jobs.
    Retries on transient connection errors (dropped Tailscale/Cloudflare tunnel,
    temporary 5xx) with a fixed delay between attempts.
    Returns the full response dict (keys: response, model, eval_count, etc.)
    """
    payload = {"model": model, "prompt": prompt, "stream": False}

    if image_path is not None:
        with open(image_path, "rb") as f:
            payload["images"] = [base64.b64encode(f.read()).decode("utf-8")]

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                f"{base_url}/api/generate",
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(retry_delay)
        except requests.HTTPError:
            # 4xx errors won't recover on retry — fail immediately
            raise

    raise last_exc


# ─── Streaming Ollama call ────────────────────────────────────────────────────

_STREAMS_DIR = Path("/tmp/runner-streams")


def call_ollama_streaming(
    base_url: str,
    model: str,
    prompt: str,
    stream_path: Path,
    image_path: Path = None,
    timeout: int = 600,
    cancel_fn=None,
) -> dict:
    """
    POST to Ollama /api/generate with stream=True.
    Writes response chunks to stream_path as they arrive (line-buffered).
    Signals completion via sidecar files alongside stream_path:
      .done       — job finished successfully
      .error      — job failed (contains error message)
      .cancelled  — job was cancelled mid-stream
    Returns the full response dict (same interface as call_ollama).
    """
    payload = {"model": model, "prompt": prompt, "stream": True}

    if image_path is not None:
        with open(image_path, "rb") as f:
            payload["images"] = [base64.b64encode(f.read()).decode("utf-8")]

    stream_path.parent.mkdir(parents=True, exist_ok=True)
    done_path = stream_path.with_suffix(".done")
    error_path = stream_path.with_suffix(".error")
    cancelled_path = stream_path.with_suffix(".cancelled")

    # Remove stale signals from a previous attempt
    for p in (done_path, error_path, cancelled_path):
        p.unlink(missing_ok=True)

    full_response = ""
    eval_count = 0

    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json=payload,
            stream=True,
            timeout=timeout,
        )
        resp.raise_for_status()

        with open(stream_path, "w", buffering=1) as sf:  # line-buffered for flush on \n
            for raw_line in resp.iter_lines():
                if cancel_fn and cancel_fn():
                    cancelled_path.touch()
                    raise JobCancelledError("cancelled during streaming")
                if not raw_line:
                    continue
                chunk_data = json.loads(raw_line)
                chunk = chunk_data.get("response", "")
                if chunk:
                    full_response += chunk
                    sf.write(chunk)
                    sf.flush()
                if chunk_data.get("done", False):
                    eval_count = chunk_data.get("eval_count", 0)
                    break

        done_path.touch()

    except JobCancelledError:
        raise
    except Exception as exc:
        error_path.write_text(str(exc))
        raise

    return {"response": full_response, "eval_count": eval_count, "model": model}


def _cleanup_stream(job_id: str) -> None:
    """Remove all stream/sidecar files for a finished job."""
    for suffix in (".txt", ".done", ".error", ".cancelled"):
        (_STREAMS_DIR / f"{job_id}{suffix}").unlink(missing_ok=True)


# ─── Discord notification ─────────────────────────────────────────────────────

def notify_discord(webhook_url: str, message: str):
    """Fire-and-forget Discord webhook. Silently skips if URL is empty."""
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"content": message}, timeout=10)
    except Exception:
        pass  # Discord failure must never kill a job


# ─── SearXNG API ─────────────────────────────────────────────────────────────

def call_searxng(query: str, cfg: dict, categories: str = None, engines: str = None) -> str:
    """
    Query SearXNG JSON API. Returns formatted markdown string of top results.
    Raises requests.HTTPError on non-2xx responses.
    Returns a placeholder string if the query yields no results.
    """
    sx = cfg.get("searxng", {})
    base_url = sx.get("base_url", "http://search.homelab.local")
    num_results = sx.get("num_results", 10)
    timeout = sx.get("timeout", 30)

    params = {
        "q": query,
        "format": "json",
        "categories": categories or sx.get("default_categories", "it"),
    }
    if engines:
        params["engines"] = engines

    resp = requests.get(f"{base_url}/search", params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])[:num_results]
    if not results:
        return f"No results found for: {query}"

    lines = [f"## Search Results: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"### {i}. {r.get('title', 'No title')}")
        lines.append(f"URL: {r.get('url', '')}")
        lines.append(r.get("content", ""))
        lines.append("")
    return "\n".join(lines)


# ─── Checklist parsing ───────────────────────────────────────────────────────

def parse_checklist_steps(content: str) -> list:
    """
    Extract unchecked checklist items from markdown body.
    Returns list of (line_index, step_text) for every '- [ ] ...' line.
    Already-completed '- [x]' and failed '- [!]' lines are skipped.
    """
    steps = []
    for i, line in enumerate(content.splitlines()):
        m = re.match(r"^- \[ \] (.+)", line)
        if m:
            steps.append((i, m.group(1).strip()))
    return steps


def update_step_in_file(file_path: Path, line_idx: int, done: bool = True):
    """
    Rewrite a single checklist line in the active job file.
    done=True  → - [x]  (complete)
    done=False → - [!]  (failed — visible in Obsidian as a distinct marker)
    """
    lines = file_path.read_text().splitlines(keepends=True)
    marker = "x" if done else "!"
    lines[line_idx] = re.sub(r"^- \[ \]", f"- [{marker}]", lines[line_idx])
    file_path.write_text("".join(lines))


# ─── Multi-runner routing ─────────────────────────────────────────────────────

def check_runner_health(base_url: str, timeout: int = 5) -> bool:
    """Return True if the Ollama endpoint is reachable and responding."""
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def resolve_ollama_config(post, cfg: dict) -> tuple:
    """
    Pick the Ollama base_url and default_model for this job.
    Priority: explicit 'runner:' field → model_runners auto-mapping → ollama fallback.
    """
    runner_name = post.get("runner", "")
    model_name = post.get("model", cfg["ollama"]["default_model"])
    runners = cfg.get("runners", {})

    # Auto-detect runner from model name if not explicitly set
    if not runner_name:
        runner_name = cfg.get("model_runners", {}).get(model_name, "")

    if runner_name and runner_name in runners:
        r = runners[runner_name]
        base_url = r.get("base_url", cfg["ollama"]["base_url"])
        default_model = r.get("default_model", cfg["ollama"]["default_model"])
    else:
        base_url = cfg["ollama"]["base_url"]
        default_model = cfg["ollama"]["default_model"]

    return base_url, default_model


# ─── Memory injection helper ─────────────────────────────────────────────────

def _inject_memory(
    memory: "MemoryManager",
    task: str,
    cfg: dict,
    post,
    logger: "RunnerLogger",
    job_id: str,
    step: int = None,
) -> str:
    """Return a context block to prepend to a prompt, or "" if memory is off.
    Handles use_memory: true (basic) and use_memory: smart (LLM-guided queries).
    """
    use_memory = post.get("use_memory")
    if not use_memory:
        return ""
    n = cfg.get("mempalace", {}).get("pre_job_results", 3)
    if use_memory == "smart":
        prior = memory.smart_search(task, cfg=cfg, n_per_query=n)
        mode  = "smart"
    else:
        prior = memory.search(task, n=n)
        mode  = "basic"
    if prior:
        kwargs = {"job_id": job_id, "chars": len(prior), "mode": mode}
        if step is not None:
            kwargs["step"] = step
        logger.emit("memory_injected", **kwargs)
    return prior


# ─── Staged job processor ─────────────────────────────────────────────────────

def process_staged_job(
    active_file: Path,
    post,
    base_url: str,
    model: str,
    cfg: dict,
    logger: RunnerLogger,
    tracer,
    memory: MemoryManager,
) -> tuple:
    """
    Run each '- [ ]' checklist step sequentially.
    Each step's output is prepended as context for the next step's prompt.
    Writes _output/{job_id}-step-NN.md after each step.
    Updates checkboxes in the active file as steps complete.
    Returns (completed_steps, total_steps).
    """
    job_id = post.get("job_id", active_file.stem)
    output_dir = Path(cfg["dirs"]["output"])
    output_dir.mkdir(parents=True, exist_ok=True)
    timeout = cfg["ollama"].get("chain_timeout", cfg["ollama"]["timeout"])

    steps = parse_checklist_steps(post.content)
    if not steps:
        raise ValueError("Staged job has no '- [ ]' steps in body")

    accumulated_context = ""
    completed = 0

    with tracer.start_as_current_span(
        "staged_job",
        attributes={"job_id": job_id, "total_steps": len(steps)},
    ):
        for step_num, (line_idx, step_text) in enumerate(steps, start=1):
            if cancel_registry.is_requested(job_id):
                raise JobCancelledError(f"Cancelled before step {step_num}")
            # Prepend prior step outputs so each step has full context
            if accumulated_context:
                prompt = (
                    f"Previous steps context:\n{accumulated_context}\n\n"
                    f"Current task: {step_text}"
                )
            else:
                prompt = step_text

            prior = _inject_memory(memory, step_text, cfg, post, logger, job_id, step=step_num)
            if prior:
                prompt = prior + prompt

            logger.emit(
                "step_started",
                job_id=job_id,
                step=step_num,
                total=len(steps),
                step_text=step_text[:80],
            )

            try:
                with tracer.start_as_current_span(
                    f"step.{step_num}",
                    attributes={"step_num": step_num, "step_text": step_text[:80]},
                ):
                    api_start = time.monotonic()
                    result = call_ollama(
                        base_url=base_url,
                        model=model,
                        prompt=prompt,
                        timeout=timeout,
                    )
                    api_ms = int((time.monotonic() - api_start) * 1000)

                response_text = result.get("response", "")
                token_count = result.get("eval_count", 0)

                step_file = output_dir / f"{job_id}-step-{step_num:02d}.md"
                step_file.write_text(
                    f"---\n"
                    f"job_id: {job_id}\n"
                    f"step: {step_num}\n"
                    f"model: {model}\n"
                    f"tokens: {token_count}\n"
                    f"completed: {datetime.now(timezone.utc).isoformat()}\n"
                    f"---\n\n"
                    f"**Step {step_num}:** {step_text}\n\n"
                    f"{response_text}\n"
                )

                memory.index(step_file)
                update_step_in_file(active_file, line_idx, done=True)
                accumulated_context += (
                    f"Step {step_num} — {step_text[:80]}:\n{response_text}\n\n"
                )

                logger.emit(
                    "step_complete",
                    job_id=job_id,
                    step=step_num,
                    tokens=token_count,
                    duration_ms=api_ms,
                )
                completed += 1

            except Exception as exc:
                logger.emit("step_failed", job_id=job_id, step=step_num, error=str(exc))
                update_step_in_file(active_file, line_idx, done=False)
                # Note the failure in context and continue — partial > total abort
                accumulated_context += (
                    f"Step {step_num} — {step_text[:80]}: FAILED — {exc}\n\n"
                )

    return completed, len(steps)


# ─── Chain job processor ─────────────────────────────────────────────────────

def process_chain_job(
    active_file: Path,
    post,
    base_url: str,
    model: str,
    cfg: dict,
    logger: RunnerLogger,
    tracer,
    memory: MemoryManager,
) -> tuple:
    """
    Execute the current step of a pre-defined chain, then spawn the next.
    Chain steps are defined as a list in the 'chain' frontmatter field.
    Each step gets only the previous step's output as context — fresh window per step.
    Spawns the next job into _queue/ automatically; writes summary when chain ends.
    Returns (step_num, chain_total, token_count, is_final).
    """
    job_id = post.get("job_id", active_file.stem)
    chain = post.get("chain", [])
    chain_index = post.get("chain_index", 0)
    chain_parent = post.get("chain_parent", job_id)
    chain_total = len(chain)
    runner_name = post.get("runner", "")
    step_num = chain_index + 1  # 1-indexed for display

    output_dir = Path(cfg["dirs"]["output"])
    output_dir.mkdir(parents=True, exist_ok=True)

    # Chain items can be plain strings or dicts with per-step overrides:
    # { prompt: "...", model: "gemma4-runbook:latest", runner: "davas" }
    # Search steps: { action: "search", query: "...", categories: "it", engines: "github" }
    step_def = chain[chain_index]
    if isinstance(step_def, str):
        step_task = step_def
        step_model = model
        step_runner = runner_name
        step_type = "text"
        step_image_rel = None
        step_action = None
    else:
        step_task = step_def.get("prompt", "")
        step_model = step_def.get("model", model)
        step_runner = step_def.get("runner", runner_name)
        step_type = step_def.get("type", "text")
        step_image_rel = step_def.get("image")
        step_action = step_def.get("action")

    # Resolve image path for vision steps
    step_image_path = None
    if step_type == "vision" and step_image_rel:
        step_image_path = Path(cfg["vault_path"]) / step_image_rel
        if not step_image_path.exists():
            raise FileNotFoundError(f"Vision image not found: {step_image_path}")

    # Resolve per-step Ollama endpoint: explicit runner > model_runners lookup > job default
    if not step_runner:
        step_runner = cfg.get("model_runners", {}).get(step_model, "")
    runners_cfg = cfg.get("runners", {})
    if step_runner and step_runner in runners_cfg:
        step_base_url = runners_cfg[step_runner].get("base_url", base_url)
    else:
        step_base_url = base_url

    # Health-check external step runner; fall back to Contabo if unreachable
    _contabo_url = cfg["ollama"]["base_url"]
    if step_base_url != _contabo_url and not check_runner_health(step_base_url):
        logger.emit("step_runner_unavailable", job_id=job_id, step=step_num, base_url=step_base_url, fallback="contabo")
        step_base_url = _contabo_url
        step_model = cfg["ollama"]["default_model"]

    # First step uses step_task directly; subsequent steps have context prepended in body
    prompt = step_task if chain_index == 0 else post.content.strip()

    prior = _inject_memory(memory, step_task, cfg, post, logger, job_id, step=step_num)
    if prior:
        prompt = prior + prompt

    logger.emit(
        "chain_step_started",
        chain_parent=chain_parent,
        step=step_num,
        total=chain_total,
        model=step_model if not step_action else None,
        action=step_action,
        job_id=job_id,
    )

    with tracer.start_as_current_span(
        f"chain_step.{step_num}",
        attributes={"chain_parent": chain_parent, "step": step_num, "total": chain_total},
    ):
        api_start = time.monotonic()
        if step_action == "search":
            step_query = step_def.get("query", step_task)
            with tracer.start_as_current_span(
                "searxng.search",
                attributes={"query": step_query, "num_results": cfg.get("searxng", {}).get("num_results", 10)},
            ):
                response_text = call_searxng(
                    step_query,
                    cfg,
                    categories=step_def.get("categories"),
                    engines=step_def.get("engines"),
                )
            token_count = 0
        else:
            result = call_ollama(
                base_url=step_base_url,
                model=step_model,
                prompt=prompt,
                image_path=step_image_path,
                timeout=cfg["ollama"].get("chain_timeout", cfg["ollama"]["timeout"]),
            )
            response_text = result.get("response", "")
            token_count = result.get("eval_count", 0)
        api_ms = int((time.monotonic() - api_start) * 1000)

    # Write step output file
    step_file = output_dir / f"{chain_parent}-step-{step_num:02d}.md"
    step_file.write_text(
        f"---\n"
        f"job_id: {job_id}\n"
        f"chain_parent: {chain_parent}\n"
        f"step: {step_num}\n"
        f"model: {step_model if not step_action else f'searxng ({step_action})'}\n"
        f"tokens: {token_count}\n"
        f"completed: {datetime.now(timezone.utc).isoformat()}\n"
        f"---\n\n"
        f"**Step {step_num}:** {step_task[:120]}\n\n"
        f"{response_text}\n"
    )

    memory.index(step_file)
    logger.emit(
        "chain_step_complete",
        chain_parent=chain_parent,
        step=step_num,
        tokens=token_count,
        duration_ms=api_ms,
    )

    next_index = chain_index + 1
    is_final = next_index >= chain_total

    if not is_final:
        # Spawn next step — context is previous output + next task prompt
        next_job_id = f"{chain_parent}-c{next_index + 1:02d}"
        next_task = chain[next_index]
        next_prompt = (
            f"Context from previous step:\n{response_text}\n\n"
            f"Current task:\n{next_task}"
        )

        next_fm = {
            "job_id": next_job_id,
            "type": "chain",
            "model": model,
            "chain": chain,
            "chain_index": next_index,
            "chain_parent": chain_parent,
        }
        if runner_name:
            next_fm["runner"] = runner_name
        if post.get("use_memory"):
            next_fm["use_memory"] = post.get("use_memory")  # preserve "smart" or True

        queue_dir = Path(cfg["dirs"]["queue"])
        queue_dir.mkdir(parents=True, exist_ok=True)
        fm_text = yaml.dump(next_fm, default_flow_style=False, allow_unicode=True)
        (queue_dir / f"{next_job_id}.md").write_text(
            f"---\n{fm_text}---\n\n{next_prompt}\n"
        )

        logger.emit(
            "chain_step_spawned",
            chain_parent=chain_parent,
            next_job=next_job_id,
            next_step=next_index + 1,
            total=chain_total,
        )

    else:
        # Final step — write summary with wikilinks to all step files
        step_links = "\n".join(
            f"- [[{chain_parent}-step-{i + 1:02d}]]" for i in range(chain_total)
        )
        summary_file = output_dir / f"{chain_parent}-output.md"
        summary_file.write_text(
            f"---\n"
            f"job_id: {chain_parent}\n"
            f"type: chain\n"
            f"steps_total: {chain_total}\n"
            f"completed: {datetime.now(timezone.utc).isoformat()}\n"
            f"---\n\n"
            f"Chain complete: **{chain_total}/{chain_total}** steps.\n\n"
            f"{step_links}\n"
        )
        memory.index(summary_file)
        logger.emit("chain_complete", chain_parent=chain_parent, total_steps=chain_total)

    return step_num, chain_total, token_count, is_final


# ─── Chain planner job processor ─────────────────────────────────────────────

_PLAN_PROMPT = (
    "You are a precise task planner. The user has given you a goal. "
    "Break it into clear, sequential steps that a language model can execute one at a time.\n\n"
    "Rules:\n"
    "- Output ONLY a valid JSON array of strings. No explanation, no preamble, no markdown code fences.\n"
    "- Each string is a complete, self-contained instruction for one step.\n"
    "- Aim for 3-6 steps. Each step should produce a concrete output that the next step builds on.\n\n"
    "Goal: {goal}"
)


def _extract_json_array(text: str) -> list:
    """Parse a JSON step list from LLM output with multiple fallback strategies.

    LLMs return plans in many formats: a proper JSON array, a fenced code block,
    one ["step"] per line, or plain numbered/bulleted prose. Each strategy below
    handles one of these cases in order of preference.
    """
    text = text.strip()
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    # Strategy 1: entire text is a valid JSON array
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Strategy 2: LLM returned one ["step"] per line — collect and merge
    # Must run before the single-block search or the first line wins early
    items = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, list):
                    items.extend(parsed)
            except json.JSONDecodeError:
                pass
    if items:
        return items

    # Strategy 3: find the first complete [...] block (handles surrounding prose)
    match = re.search(r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Strategy 4: plain prose — treat each non-empty line as a step
    # Strip common list prefixes ("1. ", "- ", "* ") and require min length
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = [re.sub(r"^[\d]+[.)]\s+|^[-*•]\s+", "", ln) for ln in lines]
    steps = [s for s in cleaned if len(s) >= 10]
    if steps:
        return steps

    raise ValueError(f"Could not extract any steps from planner output:\n{text[:400]}")


def process_chain_planner_job(
    active_file: Path,
    post,
    base_url: str,
    model: str,
    cfg: dict,
    logger: RunnerLogger,
    tracer,
    memory: MemoryManager,
) -> tuple:
    """
    LLM-driven chain: the model is asked to produce a JSON plan from the goal,
    then each generated step is executed sequentially with accumulated context.
    Writes _output/{job_id}-step-00.md (the plan) + step-NN.md per step.
    Returns (completed_steps, total_steps).
    """
    job_id = post.get("job_id", active_file.stem)
    goal = post.content.strip()
    output_dir = Path(cfg["dirs"]["output"])
    output_dir.mkdir(parents=True, exist_ok=True)
    timeout = cfg["ollama"].get("chain_timeout", cfg["ollama"]["timeout"])

    with tracer.start_as_current_span(
        "chain_planner_job",
        attributes={"job_id": job_id, "model": model},
    ):
        # Step 0: ask the model to plan
        logger.emit("planner_generating", job_id=job_id, goal=goal[:80])
        with tracer.start_as_current_span("plan.generate"):
            plan_result = call_ollama(
                base_url=base_url,
                model=model,
                prompt=_PLAN_PROMPT.format(goal=goal),
                timeout=timeout,
            )
        plan_text = plan_result.get("response", "")
        steps = _extract_json_array(plan_text)
        logger.emit("planner_plan_ready", job_id=job_id, steps=len(steps))

        # Save the generated plan as step-00 so it's inspectable
        plan_file = output_dir / f"{job_id}-step-00.md"
        plan_file.write_text(
            f"---\njob_id: {job_id}\nstep: plan\nmodel: {model}\n"
            f"tokens: {plan_result.get('eval_count', 0)}\n"
            f"completed: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
            f"**Goal:** {goal}\n\n"
            f"**Generated plan ({len(steps)} steps):**\n\n"
            + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
            + "\n"
        )

        # Execute each step with accumulated context
        accumulated_context = ""
        completed = 0
        for step_num, step_text in enumerate(steps, start=1):
            if cancel_registry.is_requested(job_id):
                raise JobCancelledError(f"Cancelled before step {step_num}")
            prompt = (
                f"Previous steps context:\n{accumulated_context}\n\nCurrent task: {step_text}"
                if accumulated_context
                else step_text
            )

            prior = _inject_memory(memory, step_text, cfg, post, logger, job_id, step=step_num)
            if prior:
                prompt = prior + prompt

            logger.emit(
                "planner_step_started",
                job_id=job_id,
                step=step_num,
                total=len(steps),
                step_text=step_text[:80],
            )
            try:
                with tracer.start_as_current_span(
                    f"plan.step.{step_num}",
                    attributes={"step_num": step_num, "step_text": step_text[:80]},
                ):
                    api_start = time.monotonic()
                    result = call_ollama(
                        base_url=base_url,
                        model=model,
                        prompt=prompt,
                        timeout=timeout,
                    )
                    api_ms = int((time.monotonic() - api_start) * 1000)

                response_text = result.get("response", "")
                token_count = result.get("eval_count", 0)
                step_file = output_dir / f"{job_id}-step-{step_num:02d}.md"
                step_file.write_text(
                    f"---\njob_id: {job_id}\nstep: {step_num}\nmodel: {model}\n"
                    f"tokens: {token_count}\nduration_ms: {api_ms}\n"
                    f"completed: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
                    f"**Task:** {step_text}\n\n{response_text}\n"
                )
                memory.index(step_file)
                accumulated_context += f"Step {step_num} — {step_text[:80]}:\n{response_text}\n\n"
                completed += 1

            except Exception as exc:
                logger.emit("planner_step_failed", job_id=job_id, step=step_num, error=str(exc))
                accumulated_context += f"Step {step_num} — {step_text[:80]}: FAILED — {exc}\n\n"

    return completed, len(steps)


# ─── Job processor ────────────────────────────────────────────────────────────

def process_job(
    active_file: Path,
    cfg: dict,
    logger: RunnerLogger,
    tracer: trace.Tracer,
    memory: MemoryManager,
    worker_id: int = 0,
):
    dirs = cfg["dirs"]
    ollama_cfg = cfg["ollama"]
    discord_webhook = cfg.get("discord", {}).get("webhook_url", "")

    # Parse YAML frontmatter + prompt body (file is already in _active/)
    post = frontmatter.load(str(active_file))
    job_id = post.get("job_id", active_file.stem)
    job_type = post.get("type", "text")  # "text", "vision", or "staged"
    image_rel = post.get("image")        # relative path from vault root (vision only)
    prompt = post.content.strip()

    # Check for cancellation before we even start (covers the race where a queued
    # job was claimed just as the web handler tried to cancel it via the filesystem)
    if cancel_registry.consume(job_id):
        move_job(active_file, Path(dirs["failed"]))
        logger.emit("job_cancelled", job_id=job_id, stage="pre_start", worker_id=worker_id)
        notify_discord(cfg.get("discord", {}).get("webhook_url", ""), f"🚫 `{job_id}` cancelled before start")
        return

    # Resolve which Ollama endpoint handles this job (supports multi-machine routing)
    base_url, default_model = resolve_ollama_config(post, cfg)
    model = post.get("model", default_model)

    # Health-check external runners; fall back silently to Contabo if unreachable
    _contabo_url = cfg["ollama"]["base_url"]
    if base_url != _contabo_url and not check_runner_health(base_url):
        logger.emit("runner_unavailable", job_id=job_id, base_url=base_url, fallback="contabo")
        base_url = _contabo_url
        model = cfg["ollama"]["default_model"]

    with tracer.start_as_current_span(
        "job",
        attributes={"job_id": job_id, "model": model, "type": job_type},
    ) as root_span:
        wall_start = time.monotonic()
        logger.emit("job_started", job_id=job_id, model=model, type=job_type, worker_id=worker_id)

        try:
            chain_final = True  # only False for intermediate chain steps

            if job_type == "chain":
                # ── Chain: one step per job, spawns next automatically ─────────
                step_num, chain_total, token_count, chain_final = process_chain_job(
                    active_file, post, base_url, model, cfg, logger, tracer, memory
                )

            elif job_type == "chain_planner":
                # ── Chain planner: LLM generates its own step list, then executes ──
                steps_done, steps_total = process_chain_planner_job(
                    active_file, post, base_url, model, cfg, logger, tracer, memory
                )
                token_count = 0

                output_dir = Path(dirs["output"])
                output_dir.mkdir(parents=True, exist_ok=True)
                step_links = f"- [[{job_id}-step-00]] *(plan)*\n" + "\n".join(
                    f"- [[{job_id}-step-{i + 1:02d}]]" for i in range(steps_total)
                )
                output_file = output_dir / f"{job_id}-output.md"
                output_file.write_text(
                    f"---\njob_id: {job_id}\ntype: chain_planner\n"
                    f"steps_total: {steps_total}\nsteps_completed: {steps_done}\n"
                    f"completed: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
                    f"Chain planner: **{steps_done}/{steps_total}** steps completed.\n\n"
                    f"{step_links}\n"
                )
                memory.index(output_file)

            elif job_type == "staged":
                # ── Staged: checklist steps in one job ───────────────────────
                steps_done, steps_total = process_staged_job(
                    active_file, post, base_url, model, cfg, logger, tracer, memory
                )
                token_count = 0  # tracked per-step inside process_staged_job

                # Write summary output with wikilinks to each step file
                output_dir = Path(dirs["output"])
                output_dir.mkdir(parents=True, exist_ok=True)
                step_links = "\n".join(
                    f"- [[{job_id}-step-{i + 1:02d}]]" for i in range(steps_total)
                )
                output_file = output_dir / f"{job_id}-output.md"
                output_file.write_text(
                    f"---\n"
                    f"job_id: {job_id}\n"
                    f"type: staged\n"
                    f"steps_total: {steps_total}\n"
                    f"steps_completed: {steps_done}\n"
                    f"completed: {datetime.now(timezone.utc).isoformat()}\n"
                    f"---\n\n"
                    f"Staged job: **{steps_done}/{steps_total}** steps completed.\n\n"
                    f"{step_links}\n"
                )
                memory.index(output_file)

            else:
                # ── Text / Vision: streaming Ollama call ──────────────────────
                image_path = None
                if job_type == "vision" and image_rel:
                    image_path = Path(cfg["vault_path"]) / image_rel
                    if not image_path.exists():
                        raise FileNotFoundError(f"Vision image not found: {image_path}")

                prior = _inject_memory(memory, prompt, cfg, post, logger, job_id)
                if prior:
                    prompt = prior + prompt

                stream_path = _STREAMS_DIR / f"{job_id}.txt"

                with tracer.start_as_current_span(
                    "ollama.generate",
                    attributes={"model": model},
                ) as ollama_span:
                    api_start = time.monotonic()
                    result = call_ollama_streaming(
                        base_url=base_url,
                        model=model,
                        prompt=prompt,
                        stream_path=stream_path,
                        image_path=image_path,
                        timeout=ollama_cfg["timeout"],
                        cancel_fn=lambda: cancel_registry.is_requested(job_id),
                    )
                    api_ms = int((time.monotonic() - api_start) * 1000)
                    token_count = result.get("eval_count", 0)
                    ollama_span.set_attribute("duration_ms", api_ms)
                    ollama_span.set_attribute("token_count", token_count)

                response_text = result.get("response", "")
                logger.emit(
                    "ollama_complete",
                    job_id=job_id,
                    model=model,
                    token_count=token_count,
                    duration_ms=api_ms,
                )

                output_dir = Path(dirs["output"])
                output_dir.mkdir(parents=True, exist_ok=True)
                output_file = output_dir / f"{job_id}-output.md"
                output_file.write_text(
                    f"---\n"
                    f"job_id: {job_id}\n"
                    f"model: {model}\n"
                    f"tokens: {token_count}\n"
                    f"completed: {datetime.now(timezone.utc).isoformat()}\n"
                    f"---\n\n"
                    f"{response_text}\n"
                )
                memory.index(output_file)

            # ── Move to completed (both paths land here) ──────────────────────
            _cleanup_stream(job_id)
            with tracer.start_as_current_span("file_move.to_completed"):
                move_job(active_file, Path(dirs["completed"]))

            duration_ms = int((time.monotonic() - wall_start) * 1000)
            root_span.set_attribute("duration_ms", duration_ms)
            root_span.set_attribute("status", "completed")

            logger.emit(
                "job_complete",
                job_id=job_id,
                model=model,
                duration_ms=duration_ms,
                token_count=token_count,
                worker_id=worker_id,
            )
            # Suppress Discord for intermediate chain steps — only ping on final
            if chain_final:
                notify_discord(
                    discord_webhook,
                    f"✅ `{job_id}` complete | model: `{model}` | tokens: {token_count} | {duration_ms}ms",
                )

        except Exception as exc:
            duration_ms = int((time.monotonic() - wall_start) * 1000)
            root_span.set_attribute("status", "failed")
            root_span.record_exception(exc)

            is_cancelled = isinstance(exc, JobCancelledError) or cancel_registry.consume(job_id)

            retries_total     = int(post.get("retries", 0))
            retries_remaining = int(post.get("retries_remaining", retries_total))
            attempt           = (retries_total - retries_remaining) + 1

            logger.emit(
                "job_failed",
                job_id=job_id,
                error=str(exc),
                duration_ms=duration_ms,
                worker_id=worker_id,
                attempt=attempt,
            )

            if not is_cancelled and retries_remaining > 0:
                # Requeue with decremented retry counter
                post["retries_remaining"] = retries_remaining - 1
                post["last_error"] = str(exc)
                post["last_failed_at"] = datetime.now(timezone.utc).isoformat()
                active_file.write_text(frontmatter.dumps(post))
                queue_dir = Path(dirs["queue"])
                queue_dir.mkdir(parents=True, exist_ok=True)
                move_job(active_file, queue_dir)
                logger.emit(
                    "job_retry_queued",
                    job_id=job_id,
                    attempt=attempt,
                    retries_remaining=retries_remaining - 1,
                    worker_id=worker_id,
                )
                notify_discord(
                    discord_webhook,
                    f"🔁 `{job_id}` attempt {attempt}/{retries_total + 1} failed, retrying "
                    f"({retries_remaining - 1} left): {exc}",
                )
            else:
                _cleanup_stream(job_id)
                with tracer.start_as_current_span("file_move.to_failed"):
                    move_job(active_file, Path(dirs["failed"]))
                if is_cancelled:
                    notify_discord(discord_webhook, f"🚫 `{job_id}` cancelled")
                else:
                    notify_discord(discord_webhook, f"❌ `{job_id}` failed: {exc}")


# ─── File watcher (polling) ───────────────────────────────────────────────────

def _recover_active(cfg: dict, logger: RunnerLogger) -> None:
    """Move any files stuck in _active/ back to _queue/ on startup.

    Files land in _active/ when the runner picks them up. If the process is
    killed mid-job they stay there and are never retried. This runs once at
    startup before the main poll loop begins.
    """
    active_dir = Path(cfg["dirs"]["active"])
    queue_dir  = Path(cfg["dirs"]["queue"])
    if not active_dir.exists():
        return
    stale = list(active_dir.glob("*.md"))
    if not stale:
        return
    queue_dir.mkdir(parents=True, exist_ok=True)
    for f in stale:
        dest = queue_dir / f.name
        f.rename(dest)
        logger.emit("startup_recovery", file=f.name, moved_to="queue")


def _claim_next_job(queue_dir: Path, active_dir: Path) -> Path | None:
    """Atomically claim the next available job from the queue.

    os.rename() (called by Path.rename) is atomic on Linux — only one thread
    wins if two workers race for the same file. The loser gets FileNotFoundError
    and moves on to the next file in sorted order.

    Returns the claimed file's path in _active/, or None if the queue is empty.
    """
    active_dir.mkdir(parents=True, exist_ok=True)
    for job_file in sorted(queue_dir.glob("*.md")):
        dest = active_dir / job_file.name
        try:
            job_file.rename(dest)
            return dest
        except FileNotFoundError:
            # Another worker claimed this file first — try the next one
            continue
    return None


def _worker_loop(
    worker_id: int,
    cfg: dict,
    logger: RunnerLogger,
    tracer: trace.Tracer,
    memory,
) -> None:
    """One polling worker: claim a job, process it, sleep if nothing available."""
    queue_dir  = Path(cfg["dirs"]["queue"])
    active_dir = Path(cfg["dirs"]["active"])
    poll_interval = cfg.get("poll_interval", 5)

    while True:
        active_file = _claim_next_job(queue_dir, active_dir)
        if active_file is not None:
            try:
                process_job(active_file, cfg, logger, tracer, memory, worker_id=worker_id)
            except Exception as exc:
                logger.emit("runner_error", error=str(exc), file=str(active_file), worker_id=worker_id)
        else:
            time.sleep(poll_interval)


def watch_queue(cfg: dict, logger: RunnerLogger, tracer: trace.Tracer):
    queue_dir = Path(cfg["dirs"]["queue"])
    queue_dir.mkdir(parents=True, exist_ok=True)
    poll_interval = cfg.get("poll_interval", 5)
    num_workers   = cfg.get("num_workers", 1)

    mem_cfg = cfg.get("mempalace", {})
    memory = MemoryManager(
        store_path=mem_cfg.get("store_path", "") if mem_cfg.get("enabled", False) else "",
        logger=logger,
        max_chars_per_result=mem_cfg.get("max_chars_per_result", 400),
    )

    _recover_active(cfg, logger)
    logger.emit(
        "runner_started",
        queue=str(queue_dir),
        poll_interval=poll_interval,
        num_workers=num_workers,
        memory_enabled=memory.enabled,
    )

    # Spawn additional workers (worker 0 is the main thread)
    for worker_id in range(1, num_workers):
        t = threading.Thread(
            target=_worker_loop,
            args=(worker_id, cfg, logger, tracer, memory),
            daemon=True,
            name=f"runner-worker-{worker_id}",
        )
        t.start()

    _worker_loop(0, cfg, logger, tracer, memory)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)

    log_file = Path(cfg["dirs"]["log_file"])
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = RunnerLogger(log_file)

    tracer = setup_tracing(
        endpoint=cfg["otel"]["endpoint"],
        service_name=cfg["otel"]["service_name"],
    )

    watch_queue(cfg, logger, tracer)
