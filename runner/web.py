#!/usr/bin/env python3
"""
Obsidian LLM Runner — Phase 5 Web UI
FastAPI + HTMX frontend for job submission and queue/output monitoring.
Instrumented with OpenTelemetry: one span per HTTP request (automatic via
FastAPIInstrumentor) plus a manual child span per job submission.
Runs on port 8000; front with Cloudflare Tunnel or reverse proxy for public access.

Deploy:
  scp -r runner/ root@<contabo>:/opt/runner/
  cd /opt/runner && python -m venv venv && venv/bin/pip install -r requirements.txt
  cp web.service /etc/systemd/system/obsidian-runner-web.service
  systemctl daemon-reload && systemctl enable --now obsidian-runner-web
"""

import asyncio
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import yaml
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from runbook import MemoryManager, cancel_registry, move_job
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.resource import ResourceAttributes


# ─── Config ───────────────────────────────────────────────────────────────────

def _load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


_config_path = os.environ.get("RUNNER_CONFIG", "config.yaml")
cfg = _load_config(_config_path)


_STREAMS_DIR = Path("/tmp/runner-streams")


# ─── OpenTelemetry ────────────────────────────────────────────────────────────

def _setup_tracing(endpoint: str, service_name: str) -> trace.Tracer:
    resource = Resource.create({ResourceAttributes.SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


tracer = _setup_tracing(
    endpoint=cfg["otel"]["endpoint"],
    service_name="obsidian-runner-ui",
)


_mem_cfg = cfg.get("mempalace", {})
memory = MemoryManager(
    store_path=_mem_cfg.get("store_path", "") if _mem_cfg.get("enabled", False) else "",
    logger=None,
)


def _current_trace_id() -> str:
    """Return the hex trace ID of the current OTel span, or empty string."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="Obsidian LLM Runner")
FastAPIInstrumentor.instrument_app(app)  # automatic span per HTTP request

templates = Jinja2Templates(directory="templates")


def _tr(request: Request, name: str, **ctx):
    """Thin wrapper using the new Starlette TemplateResponse keyword API."""
    return templates.TemplateResponse(request=request, name=name, context=ctx)


# ─── Routes ───────────────────────────────────────────────────────────────────

_STEP_FILE_RE = re.compile(r"-step-\d+$")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    models = cfg["ollama"].get("available_models", [cfg["ollama"]["default_model"]])
    runners = list(cfg.get("runners", {}).keys())
    return _tr(request, "index.html",
               models=models,
               default_model=cfg["ollama"]["default_model"],
               runners=runners)


@app.post("/jobs", response_class=HTMLResponse)
async def submit_job(
    request: Request,
    model: str = Form(...),
    job_type: str = Form("text"),
    runner: str = Form(""),
    prompt: str = Form(""),
    chain_data: str = Form(""),
    use_memory: bool = Form(False),
    retries: int = Form(0),
    image: UploadFile = None,
    attachment: UploadFile = None,
):
    """
    Write a job file to _queue/ with YAML frontmatter.
    Returns an HTMX-friendly HTML fragment showing status + trace ID.
    The submitted_trace_id is stored in frontmatter for future cross-trace
    correlation when the runner gains OTel context propagation support.
    """
    job_id = f"job-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"

    with tracer.start_as_current_span(
        "job_submission",
        attributes={"job_id": job_id, "model": model, "type": job_type},
    ):
        try:
            queue_dir = Path(cfg["dirs"]["queue"])
            queue_dir.mkdir(parents=True, exist_ok=True)

            # Handle image upload for vision jobs
            image_ref = None
            if image and image.filename:
                attachments_dir = Path(cfg["vault_path"]) / "_attachments"
                attachments_dir.mkdir(parents=True, exist_ok=True)
                img_bytes = await image.read()
                (attachments_dir / image.filename).write_bytes(img_bytes)
                image_ref = f"_attachments/{image.filename}"

            # Append text/md attachment to prompt body if provided
            if attachment and attachment.filename and job_type not in ("vision", "chain"):
                attach_text = (await attachment.read()).decode("utf-8", errors="replace")
                prompt = (
                    f"{prompt}\n\n---\nAttached: {attachment.filename}\n\n{attach_text}"
                    if prompt.strip()
                    else attach_text
                )

            tid = _current_trace_id()

            if not prompt and job_type not in ("chain", "chain_planner"):
                raise ValueError("Prompt is required")

            # Chain jobs: built by step builder UI → JSON in chain_data field
            if job_type == "chain":
                if chain_data and chain_data != "_chain_":
                    chain_steps = json.loads(chain_data)
                else:
                    chain_steps = [line.strip() for line in prompt.splitlines() if line.strip()]
                if not chain_steps:
                    raise ValueError("Chain job requires at least one step")

                # Handle per-step image uploads for vision steps
                attachments_dir = Path(cfg["vault_path"]) / "_attachments"
                form_data = await request.form()
                for step in chain_steps:
                    if not isinstance(step, dict) or step.get("type") != "vision":
                        continue
                    img_key = step.pop("_img_key", None)
                    if not img_key:
                        continue
                    img_file = form_data.get(img_key)
                    if img_file and getattr(img_file, "filename", None):
                        attachments_dir.mkdir(parents=True, exist_ok=True)
                        (attachments_dir / img_file.filename).write_bytes(
                            await img_file.read()
                        )
                        step["image"] = f"_attachments/{img_file.filename}"
                chain_yaml = yaml.dump(
                    {"chain": chain_steps}, default_flow_style=False, allow_unicode=True
                )
                fm_parts = [
                    "---",
                    f"job_id: {job_id}",
                    f"model: {model}",
                    "type: chain",
                ]
                if runner:
                    fm_parts.append(f"runner: {runner}")
                if use_memory:
                    fm_parts.append("use_memory: true")
                if retries > 0:
                    fm_parts.append(f"retries: {retries}")
                if tid:
                    fm_parts.append(f"submitted_trace_id: {tid}")
                fm_parts.append(f"submitted_at: {datetime.now(timezone.utc).isoformat()}")
                fm_parts.append(chain_yaml.rstrip())
                fm_parts.append("---")
                job_content = "\n".join(fm_parts) + "\n"
            else:
                # Build job file for text / vision / staged
                fm_lines = [
                    "---",
                    f"job_id: {job_id}",
                    f"model: {model}",
                    f"type: {job_type}",
                    f"submitted_at: {datetime.now(timezone.utc).isoformat()}",
                ]
                if runner:
                    fm_lines.append(f"runner: {runner}")
                if use_memory:
                    fm_lines.append("use_memory: true")
                if retries > 0:
                    fm_lines.append(f"retries: {retries}")
                if tid:
                    fm_lines.append(f"submitted_trace_id: {tid}")
                if image_ref:
                    fm_lines.append(f"image: {image_ref}")
                fm_lines.append("---")
                job_content = "\n".join(fm_lines) + f"\n\n{prompt}\n"

            (queue_dir / f"{job_id}.md").write_text(job_content)

            return _tr(request, "partials/submit_result.html",
                       success=True, job_id=job_id, trace_id=tid,
                       job_type=job_type, message=None)

        except Exception as exc:
            return _tr(request, "partials/submit_result.html",
                       success=False, message=str(exc),
                       trace_id=_current_trace_id())


@app.get("/api/templates")
async def get_templates():
    return cfg.get("templates", [])


@app.get("/partials/queue", response_class=HTMLResponse)
async def partial_queue(request: Request):
    queue_dir = Path(cfg["dirs"]["queue"])
    active_dir = Path(cfg["dirs"]["active"])

    queue_files = sorted(queue_dir.glob("*.md")) if queue_dir.exists() else []
    active_files = sorted(active_dir.glob("*.md")) if active_dir.exists() else []

    return _tr(request, "partials/queue.html",
               queue_files=[f.stem for f in queue_files],
               active_files=[f.stem for f in active_files])


@app.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
async def cancel_job(request: Request, job_id: str):
    """
    Cancel a queued or active job.
    Queued jobs are moved to _failed/ immediately.
    Active jobs are registered in cancel_registry; the worker checks between steps.
    Returns a refreshed queue partial so HTMX swaps the card in-place.
    """
    queue_dir  = Path(cfg["dirs"]["queue"])
    active_dir = Path(cfg["dirs"]["active"])
    failed_dir = Path(cfg["dirs"]["failed"])
    failed_dir.mkdir(parents=True, exist_ok=True)

    queue_file = queue_dir / f"{job_id}.md"

    if queue_file.exists():
        try:
            post = frontmatter.load(str(queue_file))
            post["status"] = "cancelled"
            post["cancelled_at"] = datetime.now(timezone.utc).isoformat()
            queue_file.write_text(frontmatter.dumps(post))
            move_job(queue_file, failed_dir)
        except (FileNotFoundError, OSError):
            # Worker claimed the file just as we checked — register for graceful abort
            cancel_registry.request(job_id)
    else:
        # Active or already gone — register so the worker notices between steps
        cancel_registry.request(job_id)

    queue_files  = sorted(p.stem for p in queue_dir.glob("*.md"))  if queue_dir.exists()  else []
    active_files = sorted(p.stem for p in active_dir.glob("*.md")) if active_dir.exists() else []
    return _tr(request, "partials/queue.html",
               queue_files=queue_files, active_files=active_files)


@app.get("/partials/output", response_class=HTMLResponse)
async def partial_output(request: Request):
    output_dir = Path(cfg["dirs"]["output"])
    failed_dir = Path(cfg["dirs"]["failed"])

    # Strip the '-output' suffix added by runbook.py so job IDs are clean.
    # Skip step files (job-id-step-NN.md) — they're linked from the summary output.
    output_files = []
    if output_dir.exists():
        for f in sorted(output_dir.glob("*.md"), reverse=True)[:20]:
            if _STEP_FILE_RE.search(f.stem):
                continue
            output_files.append({"stem": f.stem, "job_id": f.stem.removesuffix("-output")})

    failed = [f.stem for f in sorted(failed_dir.glob("*.md"), reverse=True)[:10]] \
        if failed_dir.exists() else []

    return _tr(request, "partials/output.html",
               output_files=output_files, failed=failed)


@app.get("/partials/logs", response_class=HTMLResponse)
async def partial_logs(request: Request):
    log_file = Path(cfg["dirs"]["log_file"])
    tail = ""
    if log_file.exists():
        lines = log_file.read_text().splitlines()
        tail = "\n".join(lines[-80:])

    return _tr(request, "partials/logs.html", log_content=tail)


_WIKILINK_RE = re.compile(r'\[\[([^\]]+)\]\]')


@app.get("/output/{file_stem}", response_class=HTMLResponse)
async def view_output(request: Request, file_stem: str):
    """Show a single output file. Accepts either 'job-id' or 'job-id-output'."""
    output_dir = Path(cfg["dirs"]["output"])
    candidates = [
        output_dir / f"{file_stem}.md",
        output_dir / f"{file_stem}-output.md",
    ]
    target = next((p for p in candidates if p.exists()), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Output '{file_stem}' not found")

    raw = target.read_text()
    # Convert Obsidian wikilinks [[job-id-step-01]] → clickable /output/ links
    content_html = _WIKILINK_RE.sub(
        lambda m: f'<a href="/output/{m.group(1)}">{m.group(1)}</a>', raw
    )

    return _tr(request, "output_detail.html",
               job_id=file_stem.removesuffix("-output"),
               content_html=content_html)


@app.get("/memory/search", response_class=HTMLResponse)
async def memory_search(request: Request, q: str = ""):
    """Search the MemPalace and return an HTML partial with results."""
    results = []
    if q and memory.enabled:
        from mempalace.searcher import search_memories
        try:
            raw = search_memories(q, palace_path=memory.store_path, n_results=5)
            results = raw.get("results", [])
        except Exception:
            pass
    return _tr(request, "partials/memory_search.html", query=q, results=results)


@app.get("/memory/status", response_class=HTMLResponse)
async def memory_status(request: Request):
    """Return a badge showing how many documents are indexed."""
    return _tr(request, "partials/memory_status.html",
               enabled=memory.enabled, count=memory.count())


@app.get("/jobs/{job_id}/live", response_class=HTMLResponse)
async def live_output(request: Request, job_id: str):
    """Live streaming view: shows output as the model generates it via SSE."""
    return _tr(request, "live_output.html", job_id=job_id)


@app.get("/api/jobs/{job_id}/stream")
async def stream_job_output(job_id: str):
    """
    SSE endpoint that tails the stream file written by runbook.py.
    Emits JSON-encoded events: {"t": "chunk"}, {"done": true}, {"error": "msg"},
    or {"cancelled": true}.

    Falls back to the completed output file if the job already finished
    before the client connected.
    """
    stream_file = _STREAMS_DIR / f"{job_id}.txt"
    done_file = _STREAMS_DIR / f"{job_id}.done"
    error_file = _STREAMS_DIR / f"{job_id}.error"
    cancelled_file = _STREAMS_DIR / f"{job_id}.cancelled"
    output_file = Path(cfg["dirs"]["output"]) / f"{job_id}-output.md"

    async def generate():
        # Already completed and stream files cleaned up — serve from output file
        if output_file.exists() and not stream_file.exists() and not done_file.exists():
            raw = output_file.read_text()
            parts = raw.split("---\n", 2)
            content = parts[2].strip() if len(parts) >= 3 else raw
            yield f"data: {json.dumps({'t': content})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

        # Wait for stream file to appear (job queued but not yet claimed)
        wait_count = 0
        while not stream_file.exists() and not done_file.exists():
            if output_file.exists():
                raw = output_file.read_text()
                parts = raw.split("---\n", 2)
                content = parts[2].strip() if len(parts) >= 3 else raw
                yield f"data: {json.dumps({'t': content})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(0.5)
            wait_count += 1
            if wait_count > 120:  # 60s timeout
                yield f"data: {json.dumps({'error': 'Timed out waiting for job to start'})}\n\n"
                return

        # Tail the stream file until a status signal appears
        pos = 0
        idle_count = 0
        while True:
            if cancelled_file.exists():
                if stream_file.exists():
                    tail = stream_file.read_text()[pos:]
                    if tail:
                        yield f"data: {json.dumps({'t': tail})}\n\n"
                yield f"data: {json.dumps({'cancelled': True})}\n\n"
                return

            if error_file.exists():
                if stream_file.exists():
                    tail = stream_file.read_text()[pos:]
                    if tail:
                        yield f"data: {json.dumps({'t': tail})}\n\n"
                err = error_file.read_text() if error_file.exists() else "unknown error"
                yield f"data: {json.dumps({'error': err})}\n\n"
                return

            try:
                if stream_file.exists():
                    content = stream_file.read_text()
                    new = content[pos:]
                    if new:
                        idle_count = 0
                        pos = len(content)
                        yield f"data: {json.dumps({'t': new})}\n\n"
            except OSError:
                pass

            # Check done *after* reading (ensures last chunk is flushed first)
            if done_file.exists():
                if stream_file.exists():
                    tail = stream_file.read_text()[pos:]
                    if tail:
                        yield f"data: {json.dumps({'t': tail})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
                return

            idle_count += 1
            if idle_count > 3000:  # ~5 min of no activity
                yield f"data: {json.dumps({'error': 'Stream timeout'})}\n\n"
                return

            await asyncio.sleep(0.1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable Nginx/Cloudflare buffering
            "Connection": "keep-alive",
        },
    )
