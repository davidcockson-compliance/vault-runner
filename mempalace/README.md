# MemPalace

Semantic memory store over past job outputs, vault notes, and a curated book corpus. Exposed over MCP (Model Context Protocol) so both vault-runner and Claude Code share one memory.

MemPalace itself is a separate project and is **not bundled in this repo** — this directory just describes how vault-runner consumes it. Point `mempalace.store_path` in `runner/config.yaml` at an existing MemPalace store.

## How vault-runner uses it

1. **Pre-job context injection** — jobs with `use_memory: true` in frontmatter trigger a query against the store. The top-N matching past outputs (default 3, capped at 400 chars each) are prepended to the prompt as context.
2. **Post-job indexing** — every completed output is added back to the store under the `runner-outputs` wing, so tomorrow's jobs see today's answers.
3. **Smart queries** — a small local model (`qwen2.5:7b` by default) rewrites the user prompt into multiple targeted search queries, deduplicates results across them, and injects the combined set. This catches context the naive single-query approach misses.

## Architecture

```
┌──────────────┐   MCP      ┌────────────────┐
│ vault-runner │ ─────────▶ │   MemPalace    │
└──────────────┘            │   MCP server   │
┌──────────────┐   MCP      │                │
│ Claude Code  │ ─────────▶ │   (19 tools)   │
└──────────────┘            └────────┬───────┘
                                     │
                                     ▼
                            ┌────────────────┐
                            │  Store on disk │
                            │  (drawers,     │
                            │   rooms, wings)│
                            └────────────────┘
```

Drawers are the unit of memory. Rooms group drawers by topic. Wings group rooms by source (e.g. `books/sre`, `runner-outputs`, `vault/projects`).

## Config (in `../runner/config.yaml`)

```yaml
mempalace:
  enabled: true
  store_path: /path/to/mempalace-store
  pre_job_results: 3
  max_chars_per_result: 400
  smart_query_model: qwen2.5:7b
  smart_query_results_per_query: 2
```

## Operational notes

- **Scale matters.** The palace was rebuilt after ballooning to ~130K drawers across mixed content broke query latency. Current healthy configuration is a curated corpus (≈9K drawers) plus live runner outputs. Keep wings narrow and topical; don't bulk-import everything.
- **Nightly mining** of the book corpus runs as a cron on the host; runner outputs are indexed inline at job completion time (one file at a time, safe under load).
- **Graceful degradation.** If `mempalace.enabled: false` or the store is unreachable, jobs with `use_memory: true` still run — they just don't get context injected and a warning lands in `RUNNER-LOG.md`.
