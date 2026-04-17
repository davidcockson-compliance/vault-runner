# Job types

Every job is a Markdown file in `_queue/` with YAML frontmatter. The `type:` field selects the execution path.

## `text` — single prompt

```markdown
---
type: text
model: qwen2.5:14b
use_memory: false   # optional, default false
---
Write a short summary of the Raft consensus algorithm.
```

Body of the file is the prompt. Output is streamed to `runner-outputs/<job-id>-output.md`.

## `vision` — prompt + image

```markdown
---
type: vision
model: gemma3:4b
image: /path/to/screenshot.png
---
What is shown in this image? List any text verbatim.
```

Requires a multimodal model.

## `staged` — multi-step checklist

```markdown
---
type: staged
model: qwen2.5:14b
stages:
  - Extract all claims from the document below.
  - Fact-check each claim against common knowledge.
  - Produce a final confidence rating per claim.
---
<document body>
```

Each stage sees the output of the previous one appended to its prompt. Good for linear refinement tasks when you don't need per-step routing.

## `chain` — multi-step pipeline with per-step routing

```markdown
---
type: chain
steps:
  - action: search
    query: "current state of small language models 2026"
  - prompt: "Analyse the search results above."
    model: qwen2.5:14b
  - prompt: "Produce a 5-bullet executive summary."
    model: gemma3:4b
---
```

Each step becomes its own queue file carrying accumulated context. Steps can route to different machines via `model_runners`. Most powerful for heterogeneous workloads.

## `chain_planner` — LLM generates the chain

```markdown
---
type: chain_planner
goal: "Research and write a brief on observability tooling for hobby homelabs."
model: qwen2.5:14b
---
```

The planner LLM emits a `chain:` block, which is then executed as a normal chain job. Use when you don't want to hand-design the pipeline.

## Common frontmatter

| Field | Applies to | Purpose |
|---|---|---|
| `type` | all | job kind |
| `model` | all | default model for steps without their own |
| `use_memory` | all | inject MemPalace results into prompt |
| `runner` | all | override model-based runner routing |
| `priority` | all | higher = picked up first |
| `parent_job` | chain steps | back-pointer for trace correlation |

## Chain actions

In addition to `prompt:` steps, chain jobs support:

- `action: search` — query SearXNG, inject results as context for next step.
- `action: read_file` — inject file contents.
- `action: memory_query` — explicit MemPalace query (as opposed to auto-injection).

## Cancellation

A running job can be cancelled from the UI. The cancellation registry sets a flag; the executor checks it between steps and between token batches, so in-flight generations stop promptly.
