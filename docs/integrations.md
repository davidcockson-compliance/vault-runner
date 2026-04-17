# Integrations

vault-runner is small glue; most of the capability comes from composing it with other self-hosted tools. This doc describes how each integration plugs in and what it buys you.

## Obsidian

The vault is the source of truth. Jobs are notes; outputs are notes; the log is a note. Obsidian's graph view, tags, and search all work on runner artefacts for free.

**Touch points:** `vault_path`, all `dirs:` entries, `log_file`.

## Syncthing

Bi-directional folder sync across machines. Write a job from your laptop, it appears on the VPS, runs, output syncs back. No API, no polling loop.

**Touch points:** none in code — operates on the filesystem beneath the runner.

## Tailscale

Zero-trust mesh between machines. The runner reaches secondary Ollama instances at their Tailscale IPs; SSH and Syncthing run over it too.

**Touch points:** `runners.*.base_url` set to Tailscale IPs.

## Cloudflare Tunnel

Exposes the FastAPI UI publicly without opening ports on the VPS. Attaches auth / access rules at Cloudflare's edge.

**Touch points:** none in code — runs as a `cloudflared` sidecar.

## Ollama

Local model runtime. The runner speaks the native Ollama HTTP API for streaming generation.

**Touch points:** `ollama.base_url`, `runners.*.base_url`.

## MemPalace (bundled in [../mempalace](../mempalace))

Vector store + MCP server over the vault and past job outputs. Two integration points:

1. **Pre-job injection** — `use_memory: true` pulls the top-N matching past outputs into the prompt.
2. **Post-job indexing** — each completed output is added back to the store, so tomorrow's jobs see today's answers.

Also exposed to Claude Code via MCP, so the same memory is queryable from your dev environment.

**Touch points:** `mempalace.*` config block; `runner/memory.py` client.

## SearXNG

Self-hosted metasearch. The `action: search` chain step calls SearXNG and injects the top-N results as context for the next step.

**Touch points:** `searxng.*` config block; `runner/search.py`.

## OpenTelemetry → Tempo + Grafana

Every job is a trace. Every LLM call, MemPalace query, and SearXNG fetch is a span with token counts, latencies, and step metadata. Grafana's trace viewer renders them as flamegraphs.

**Touch points:** `otel.*` config; spans emitted throughout `runbook.py`.

## Discord

Failure-only webhook. On any unhandled exception or job marked `failed`, the webhook posts the traceback and a trace link.

**Touch points:** `discord.webhook_url`; `runner/alerts.py`.

## Summary

```
Obsidian ────────▶ vault (truth)
Syncthing ───────▶ cross-machine replication
Tailscale ───────▶ private network between runners
Cloudflare ──────▶ public edge for the UI
Ollama ──────────▶ LLM runtime (per machine)
MemPalace (MCP) ─▶ semantic memory (pre/post)
SearXNG ─────────▶ web search (in-chain)
OTel → Tempo ────▶ tracing
Discord ─────────▶ alerting
```

Each integration is optional and gated by a config flag. The runner works end-to-end with just Ollama and a vault folder.
