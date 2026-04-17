---
type: chain
model: qwen2.5:14b
use_memory: true
steps:
  - prompt: |
      The vault context above is drawn from your knowledge base on this topic.
      Synthesise: list key facts, frameworks, definitions, and open questions.
    model: qwen2.5:14b
  - prompt: |
      Using the briefing above, write a structured answer with:
      - What is known
      - Knowledge gaps
      - Most important concepts
      - Suggested follow-ups
    model: qwen2.5:14b
---
Ask your question here.
