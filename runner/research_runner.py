"""
LangGraph research pipeline for vault-runner.

Triggered by job_type: research in job frontmatter. Runs a multi-step agentic loop:
  1. plan_queries  — LLM generates a list of search queries from the research goal
  2. search        — parallel Tavily searches for each query
  3. extract       — LLM extracts entities and relations from search results
  4. store         — writes entity/relation triples into KuzuDB for graph persistence
  5. synthesise    — LLM writes a structured research report from accumulated knowledge
  6. should_continue — decides whether another search round is needed (up to max_iterations)

The KuzuDB graph accumulates knowledge across runs — future research jobs on related
topics can query it to avoid redundant searches.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TypedDict

import kuzu
import yaml
from langchain_core.messages import HumanMessage
from langfuse.decorators import observe
from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)


# ─── Graph state ──────────────────────────────────────────────────────────────

class ResearchState(TypedDict):
    goal: str
    queries: list[str]
    search_results: list[dict]           # {"query": str, "content": str}
    extracted_triples: list[dict]        # {"subject": str, "predicate": str, "object": str}
    iteration: int
    max_iterations: int
    report: str
    done: bool


# ─── KuzuDB helpers ───────────────────────────────────────────────────────────

def _init_kuzu(db_path: str) -> kuzu.Connection:
    db = kuzu.Database(db_path)
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE IF NOT EXISTS Entity(name STRING, PRIMARY KEY (name))")
    conn.execute(
        "CREATE REL TABLE IF NOT EXISTS Relation(FROM Entity TO Entity, predicate STRING, source STRING)"
    )
    return conn


def _store_triples(conn: kuzu.Connection, triples: list[dict], source: str) -> None:
    for triple in triples:
        subject = triple.get("subject", "").strip()
        predicate = triple.get("predicate", "").strip()
        obj = triple.get("object", "").strip()
        if not (subject and predicate and obj):
            continue
        conn.execute("MERGE (e:Entity {name: $name})", {"name": subject})
        conn.execute("MERGE (e:Entity {name: $name})", {"name": obj})
        conn.execute(
            "MATCH (s:Entity {name: $s}), (o:Entity {name: $o}) "
            "CREATE (s)-[:Relation {predicate: $p, source: $src}]->(o)",
            {"s": subject, "o": obj, "p": predicate, "src": source},
        )


# ─── LLM call helper ─────────────────────────────────────────────────────────

def _call_llm(prompt: str, cfg: dict) -> str:
    """Route to cloud provider or Ollama based on model_providers config."""
    research_cfg = cfg.get("langgraph_research", {})
    model = research_cfg.get("default_model", cfg.get("ollama", {}).get("default_model", "qwen2.5:14b"))
    provider = cfg.get("model_providers", {}).get(model)

    if provider and provider in cfg:
        from runbook import call_cloud_provider
        result = call_cloud_provider(provider, cfg[provider], model, prompt)
    else:
        from runbook import call_ollama
        result = call_ollama(
            base_url=cfg["ollama"]["base_url"],
            model=model,
            prompt=prompt,
            timeout=cfg["ollama"].get("chain_timeout", 900),
        )
    return result.get("response", "")


# ─── Graph nodes ──────────────────────────────────────────────────────────────

@observe(name="plan_queries")
def plan_queries(state: ResearchState, cfg: dict) -> ResearchState:
    """Generate a set of search queries from the research goal."""
    prompt = (
        f"You are a research assistant. Generate {3 + state['iteration']} specific, "
        f"diverse search queries to thoroughly investigate this topic.\n\n"
        f"Topic: {state['goal']}\n\n"
        f"Already searched: {[r['query'] for r in state['search_results']]}\n\n"
        "Output a JSON array of query strings only. No explanation."
    )
    raw = _call_llm(prompt, cfg)
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        queries = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        queries = [state["goal"]]

    return {**state, "queries": queries}


@observe(name="search")
def search(state: ResearchState, cfg: dict) -> ResearchState:
    """Run Tavily searches for each query and accumulate results."""
    from tavily import TavilyClient
    research_cfg = cfg.get("research", {})
    api_key_env = research_cfg.get("tavily_api_key_env", "TAVILY_API_KEY")
    client = TavilyClient(api_key=os.environ[api_key_env])
    max_results = research_cfg.get("max_results", 5)

    new_results = list(state["search_results"])
    for query in state["queries"]:
        try:
            resp = client.search(query=query, search_depth="basic", max_results=max_results)
            content = "\n\n".join(
                f"**{r['title']}** ({r['url']})\n{r['content']}"
                for r in resp.get("results", [])
            )
            new_results.append({"query": query, "content": content})
        except Exception as exc:
            logger.warning("Tavily search failed for %r: %s", query, exc)

    return {**state, "search_results": new_results}


@observe(name="extract_knowledge")
def extract_knowledge(state: ResearchState, cfg: dict) -> ResearchState:
    """Extract entity/relation triples from the latest search results."""
    # Only process results from the current iteration
    iteration_results = state["search_results"][-(len(state["queries"])):]
    combined = "\n\n".join(r["content"] for r in iteration_results)[:6000]

    prompt = (
        "Extract factual entity-relation triples from the text below.\n\n"
        "Output a JSON array of objects with keys: subject, predicate, object.\n"
        "Keep values short (under 60 chars each). Extract 10–20 triples.\n\n"
        f"Text:\n{combined}\n\nJSON array:"
    )
    raw = _call_llm(prompt, cfg)
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        triples = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        triples = []

    return {**state, "extracted_triples": state["extracted_triples"] + triples}


@observe(name="store_knowledge")
def store_knowledge(state: ResearchState, cfg: dict, conn: kuzu.Connection) -> ResearchState:
    """Persist extracted triples into KuzuDB."""
    _store_triples(conn, state["extracted_triples"], source=state["goal"][:100])
    return state


@observe(name="synthesise")
def synthesise(state: ResearchState, cfg: dict) -> ResearchState:
    """Write a structured research report from all accumulated search results."""
    all_content = "\n\n---\n\n".join(
        f"Query: {r['query']}\n{r['content']}" for r in state["search_results"]
    )[:12000]

    entity_summary = ""
    if state["extracted_triples"]:
        sample = state["extracted_triples"][:20]
        entity_summary = "\n".join(f"- {t['subject']} → {t['predicate']} → {t['object']}" for t in sample)
        entity_summary = f"\n\nKey entities and relations extracted:\n{entity_summary}"

    prompt = (
        f"You are a research analyst. Write a comprehensive, structured report on:\n\n"
        f"**{state['goal']}**\n\n"
        f"Based on the following research:{entity_summary}\n\n"
        f"Source material:\n{all_content}\n\n"
        "Report structure:\n"
        "1. Executive Summary (3–5 sentences)\n"
        "2. Key Findings (bullet points with source citations)\n"
        "3. Detailed Analysis (2–4 paragraphs)\n"
        "4. Knowledge Gaps and Uncertainties\n"
        "5. Recommended Next Steps"
    )
    report = _call_llm(prompt, cfg)
    return {**state, "report": report, "done": True}


def should_continue(state: ResearchState) -> str:
    """Route: run another search iteration or proceed to synthesis."""
    if state["iteration"] >= state["max_iterations"] or state.get("done"):
        return "synthesise"
    return "plan_queries"


# ─── Graph assembly ───────────────────────────────────────────────────────────

def build_graph(cfg: dict, conn: kuzu.Connection):
    graph = StateGraph(ResearchState)

    graph.add_node("plan_queries",      lambda s: plan_queries(s, cfg))
    graph.add_node("search",            lambda s: search(s, cfg))
    graph.add_node("extract_knowledge", lambda s: extract_knowledge(s, cfg))
    graph.add_node("store_knowledge",   lambda s: store_knowledge(s, cfg, conn))
    graph.add_node("synthesise",        lambda s: synthesise(s, cfg))

    graph.set_entry_point("plan_queries")
    graph.add_edge("plan_queries",      "search")
    graph.add_edge("search",            "extract_knowledge")
    graph.add_edge("extract_knowledge", "store_knowledge")
    graph.add_conditional_edges("store_knowledge", should_continue, {
        "plan_queries": "plan_queries",
        "synthesise":   "synthesise",
    })
    graph.add_edge("synthesise", END)

    return graph.compile()


# ─── Public entry point ───────────────────────────────────────────────────────

@observe(name="research_job")
def run_research_job(goal: str, cfg: dict) -> str:
    """
    Run the full research pipeline for a given goal.
    Called from runbook.py when job_type == 'research'.
    Returns the synthesised report as a markdown string.
    """
    research_cfg = cfg.get("langgraph_research", {})
    kuzu_path = research_cfg.get("kuzu_db_path", "/tmp/research-graph")
    max_iterations = research_cfg.get("max_search_iterations", 3)

    Path(kuzu_path).mkdir(parents=True, exist_ok=True)
    conn = _init_kuzu(kuzu_path)

    compiled = build_graph(cfg, conn)
    initial_state: ResearchState = {
        "goal": goal,
        "queries": [],
        "search_results": [],
        "extracted_triples": [],
        "iteration": 0,
        "max_iterations": max_iterations,
        "report": "",
        "done": False,
    }

    final_state = compiled.invoke(initial_state)
    return final_state.get("report", "Research pipeline produced no output.")
