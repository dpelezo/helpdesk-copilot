import os
import json
import httpx
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END
from langsmith import traceable


# ── State schema ──────────────────────────────────────────────
class AgentState(TypedDict):
    ticket_id: str
    subject: str
    description: str
    conversation: list[dict]
    resolved_by: Optional[str]
    resolution_note: Optional[str]
    reviewer_feedback: Optional[str]  # ← new
    existing_kb: list[dict]
    similarity_score: float
    duplicate_of: Optional[str]
    draft_title: str
    draft_slug: str  # ← new
    draft_content: str  # ← new (replaces draft_resolution)
    draft_excerpt: str  # ← new
    draft_is_internal: bool  # ← new
    draft_problem: str
    draft_resolution: str
    draft_tags: list[str]
    error: Optional[str]


# ── Fireworks LLM call via httpx (no SDK import issues) ──────
@traceable
def llm_call(prompt: str, max_tokens: int = 1024) -> str:
    api_key = os.getenv("FIREWORKS_API_KEY", "")
    response = httpx.post(
        "https://api.fireworks.ai/inference/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "accounts/fireworks/models/qwen3-vl-30b-a3b-instruct",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        },
        timeout=60,
    )
    return response.json()["choices"][0]["message"]["content"].strip()


# ── Fireworks embeddings for duplicate check ──────────────────
def get_embedding(text: str) -> list[float]:
    api_key = os.getenv("FIREWORKS_API_KEY", "")
    resp = httpx.post(
        "https://api.fireworks.ai/inference/v1/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "nomic-ai/nomic-embed-text-v1.5",
            "input": text,
        },
        timeout=15,
    )
    return resp.json()["data"][0]["embedding"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    import numpy as np

    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


# ── Node 1: Analyze ticket ────────────────────────────────────
@traceable
def analyze_ticket(state: AgentState) -> AgentState:
    # Format the conversation for the prompt
    convo_history = "\n".join(
        [f"{m['role'].upper()}: {m['content']}" for m in state.get("conversation", [])]
    )
    reviewer_feedback = state.get("reviewer_feedback", "")
    resolution_note = state.get("resolution_note")
    feedback_section = ""
    if reviewer_feedback:
        feedback_section = f"""
⚠️ REVIEWER FEEDBACK — you MUST incorporate this feedback in your analysis:
{reviewer_feedback}
"""

    prompt = f"""You are a support knowledge base expert. Analyze this support ticket conversation.

Ticket Subject: {state['subject']}
Initial Description: {state['description']}
{f'Agent Resolution Note: {resolution_note}' if resolution_note else ''}
{feedback_section}
Full Conversation Thread:
{convo_history}

Extract:
1. The core problem the customer experienced
2. The exact resolution steps from agent messages
3. Root cause
4. Relevant tags

Return ONLY a JSON object:
- problem_summary: clear description (2-3 sentences)
- resolution_summary: step-by-step resolution from agent messages
- root_cause: what caused the issue
- tags: list of 3-5 tags

Return ONLY valid JSON, no markdown fences."""

    try:
        raw = llm_call(prompt, max_tokens=1024)
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        return {
            **state,
            "draft_problem": parsed.get("problem_summary", state["description"]),
            "draft_resolution": parsed.get("resolution_summary", ""),
            "draft_tags": parsed.get("tags", []),
            "error": None,
        }
    except Exception as e:
        return {**state, "error": f"analyze_ticket failed: {e}"}


# ── Node 2: Check duplicates ──────────────────────────────────
@traceable
def check_duplicates(state: AgentState) -> AgentState:
    if not state.get("existing_kb"):
        return {**state, "similarity_score": 0.0, "duplicate_of": None}
    try:
        query = f"{state['draft_problem']} {state['draft_resolution']}"
        query_emb = get_embedding(query)

        best_score = 0.0
        best_id = None
        for kb in state["existing_kb"]:
            text = f"{kb['problem']} {kb.get('content', '')}"
            emb = get_embedding(text)
            score = cosine_similarity(query_emb, emb)
            if score > best_score:
                best_score = score
                best_id = kb["id"]

        duplicate_of = best_id if best_score > 0.85 else None
        return {**state, "similarity_score": best_score, "duplicate_of": duplicate_of}
    except Exception:
        return {**state, "similarity_score": 0.0, "duplicate_of": None}


# ── Node 3: Generate KB draft ─────────────────────────────────
@traceable
def generate_kb_draft(state: AgentState) -> AgentState:
    note = (
        f"\n\n> ⚠️ Possible duplicate of KB {state['duplicate_of']}"
        if state.get("duplicate_of")
        else ""
    )
    feedback_section = (
        f"\n\nReviewer Feedback to incorporate:\n{state.get('reviewer_feedback', '')}"
        if state.get("reviewer_feedback")
        else ""
    )

    prompt = f"""You are a technical writer creating a knowledge base article for a support team helpdesk system.

Problem Summary: {state['draft_problem']}
Resolution Steps: {state['draft_resolution']}
Tags: {', '.join(state.get('draft_tags', []))}
{feedback_section}

Write a professional KB article and return ONLY a JSON object with these exact keys:

- title: concise searchable title (max 10 words)
- content: full markdown article with these sections:
    ## Problem
    (clear description of what the customer experiences)
    
    ## Root Cause
    (what caused the issue)
    
    ## Resolution
    (numbered step-by-step fix)
    
    ## Prevention
    (optional tip to prevent recurrence)
    
- excerpt: one sentence summary (max 20 words)
- is_internal: false

Return ONLY valid JSON, no markdown fences, no extra text."""

    try:
        raw = llm_call(prompt, max_tokens=2000)
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)

        from app.db.models import slugify

        title = parsed.get("title", state["subject"])
        content = parsed.get("content", "") + note

        return {
            **state,
            "draft_title": title,
            "draft_slug": slugify(title),
            "draft_content": content,
            "draft_excerpt": parsed.get("excerpt", ""),
            "draft_is_internal": parsed.get("is_internal", False),
            "draft_problem": state["draft_problem"],
            "draft_resolution": state["draft_resolution"],
        }
    except Exception as e:
        return {**state, "error": f"generate_kb_draft failed: {e}"}


# ── Conditional edge ──────────────────────────────────────────
def should_continue(state: AgentState) -> str:
    if state.get("error"):
        return END
    return "check_duplicates"


# ── Build graph ───────────────────────────────────────────────
@traceable
def build_kb_graph():
    graph = StateGraph(AgentState)
    graph.add_node("analyze_ticket", analyze_ticket)
    graph.add_node("check_duplicates", check_duplicates)
    graph.add_node("generate_kb_draft", generate_kb_draft)

    graph.set_entry_point("analyze_ticket")
    graph.add_conditional_edges(
        "analyze_ticket",
        should_continue,
        {
            "check_duplicates": "check_duplicates",
            END: END,
        },
    )
    graph.add_edge("check_duplicates", "generate_kb_draft")
    graph.add_edge("generate_kb_draft", END)

    return graph.compile()


kb_graph = build_kb_graph()


async def run_kb_agent(ticket_data: dict, existing_kb: list[dict]) -> AgentState:
    initial_state: AgentState = {
        "ticket_id": ticket_data["ticket_id"],
        "subject": ticket_data["subject"],
        "description": ticket_data["description"],
        "conversation": ticket_data.get("conversation", []),
        "resolved_by": ticket_data.get("resolved_by"),
        "resolution_note": ticket_data.get("resolution_note"),
        "reviewer_feedback": ticket_data.get("reviewer_feedback"),
        "existing_kb": existing_kb,
        "similarity_score": 0.0,
        "duplicate_of": None,
        "draft_title": "",
        "draft_slug": "",
        "draft_content": "",
        "draft_excerpt": "",
        "draft_is_internal": False,
        "draft_problem": "",
        "draft_resolution": "",
        "draft_tags": [],
        "error": None,
    }
    result = await kb_graph.ainvoke(initial_state)
    return result
