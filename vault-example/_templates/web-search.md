---
type: chain
model: qwen2.5:14b
steps:
  - action: search
    query: ""            # fill me in
    categories: it
  - prompt: |
      Analyse the search results above. Extract the most relevant and reliable information.
      List key findings with source URLs. Flag thin coverage or conflicting sources.
    model: qwen2.5:14b
  - prompt: |
      Using the analysis above, write a clear structured answer to the original query.
      Include key conclusions, caveats, and 3–5 next steps.
    model: qwen2.5:14b
---
