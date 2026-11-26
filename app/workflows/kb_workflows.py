from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from datetime import timedelta
from typing import Optional

# ── Activities ────────────────────────────────────────────────


@activity.defn
async def fetch_existing_kb(topic_hint: str) -> list[dict]:
    from app.db.models import KBArticle, AsyncSessionLocal
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(KBArticle).where(KBArticle.status == "approved").limit(50)
        )
        articles = result.scalars().all()
        return [
            {"id": a.id, "problem": a.problem, "content": a.content} for a in articles
        ]


@activity.defn
async def run_agent_activity(ticket_data: dict, existing_kb: list[dict]) -> dict:
    from app.agents.kb_agent import run_kb_agent

    result = await run_kb_agent(ticket_data, existing_kb)
    return dict(result)


@activity.defn
async def save_draft_to_db(draft: dict, workflow_id: str) -> str:
    import uuid
    from app.db.models import KBArticle, AsyncSessionLocal, KBStatus, slugify

    article_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        article = KBArticle(
            id=article_id,
            ticket_id=draft["ticket_id"],
            title=draft["draft_title"],
            slug=draft.get("draft_slug") or slugify(draft["draft_title"]),
            content=draft.get("draft_content", ""),
            excerpt=draft.get("draft_excerpt", ""),
            is_internal=draft.get("draft_is_internal", False),
            problem=draft.get("draft_problem", ""),
            tags=",".join(draft.get("draft_tags", [])),
            status=KBStatus.pending_approval,
            similarity_score=draft.get("similarity_score"),
            duplicate_of=draft.get("duplicate_of"),
            workflow_id=workflow_id,
            resolved_by=draft.get("resolved_by"),
        )
        session.add(article)
        await session.commit()
    return article_id


@activity.defn
async def notify_human_agent(article_id: str, draft: dict) -> None:
    import os, httpx

    webhook_url = os.getenv("NOTIFICATION_WEBHOOK_URL")
    if not webhook_url:
        return
    payload = {
        "text": "📋 New KB Draft awaiting approval",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{draft.get('draft_title', 'KB Draft')}*\n{str(draft.get('draft_problem', ''))[:200]}...",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Review at: `PATCH /api/v1/kb-drafts/{article_id}/review`",
                },
            },
        ],
    }
    async with httpx.AsyncClient() as client:
        await client.post(webhook_url, json=payload, timeout=10)


@activity.defn
async def update_draft_with_feedback(
    article_id: str, feedback: str, new_draft: dict
) -> None:
    from app.db.models import KBArticle, AsyncSessionLocal, KBStatus, slugify
    from sqlalchemy import update

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(KBArticle)
            .where(KBArticle.id == article_id)
            .values(
                title=new_draft.get("draft_title"),
                slug=new_draft.get("draft_slug")
                or slugify(new_draft.get("draft_title", "")),
                content=new_draft.get("draft_content"),
                excerpt=new_draft.get("draft_excerpt"),
                reviewer_feedback=feedback,
                status=KBStatus.pending_approval,
            )
        )
        await session.commit()


@activity.defn
async def publish_kb_article(article_id: str) -> None:
    from app.db.models import KBArticle, AsyncSessionLocal, KBStatus
    from sqlalchemy import update

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(KBArticle)
            .where(KBArticle.id == article_id)
            .values(status=KBStatus.approved)
        )
        await session.commit()


@activity.defn
async def reject_kb_article(article_id: str) -> None:
    from app.db.models import KBArticle, AsyncSessionLocal, KBStatus
    from sqlalchemy import update

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(KBArticle)
            .where(KBArticle.id == article_id)
            .values(status=KBStatus.rejected)
        )
        await session.commit()


# ── Workflow ───────────────────────────────────────────────────


@workflow.defn
class KBCreationWorkflow:
    def __init__(self):
        self._decision: Optional[dict] = None

    @workflow.signal
    async def human_decision(self, decision: dict) -> None:
        self._decision = decision

    @workflow.run
    async def run(self, ticket_data: dict) -> dict:
        retry = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))
        workflow_id = workflow.info().workflow_id

        # ── Step 1: fetch existing KB for dedup ───────────────
        existing_kb = await workflow.execute_activity(
            fetch_existing_kb,
            ticket_data.get("subject", ""),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )

        # ── Step 2: run AI agent ──────────────────────────────
        draft = await workflow.execute_activity(
            run_agent_activity,
            args=[ticket_data, existing_kb],
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=retry,
        )

        if draft.get("error"):
            return {"status": "error", "error": draft["error"]}

        # ── Step 3: save draft and notify reviewer ────────────
        article_id = await workflow.execute_activity(
            save_draft_to_db,
            args=[draft, workflow_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )

        await workflow.execute_activity(
            notify_human_agent,
            args=[article_id, draft],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry,
        )

        # ── Step 4: review loop — runs until accept or reject ─
        # Keeps the current ticket_data so feedback accumulates
        current_ticket_data = dict(ticket_data)

        while True:
            # Reset and wait for next human decision
            self._decision = None
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(days=7),
            )

            action = self._decision.get("action", "")
            feedback = self._decision.get("feedback", "")

            # ── Accept without changes ────────────────────────
            if action == "accept":
                await workflow.execute_activity(
                    publish_kb_article,
                    article_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry,
                )
                return {"status": "published", "article_id": article_id}

            # ── Accept with feedback applied then publish ─────
            elif action == "accept_with_feedback":
                current_ticket_data["reviewer_feedback"] = feedback
                improved_draft = await workflow.execute_activity(
                    run_agent_activity,
                    args=[current_ticket_data, existing_kb],
                    start_to_close_timeout=timedelta(minutes=3),
                    retry_policy=retry,
                )
                await workflow.execute_activity(
                    update_draft_with_feedback,
                    args=[article_id, feedback, improved_draft],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry,
                )
                await workflow.execute_activity(
                    publish_kb_article,
                    article_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry,
                )
                return {"status": "published_with_feedback", "article_id": article_id}

            # ── Regenerate — AI improves draft, stays in review─
            elif action == "regenerate":
                # Pass feedback to agent so it improves the draft
                current_ticket_data["reviewer_feedback"] = feedback
                regenerated_draft = await workflow.execute_activity(
                    run_agent_activity,
                    args=[current_ticket_data, existing_kb],
                    start_to_close_timeout=timedelta(minutes=3),
                    retry_policy=retry,
                )
                # Update DB with improved draft, stays pending_approval
                await workflow.execute_activity(
                    update_draft_with_feedback,
                    args=[article_id, feedback, regenerated_draft],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry,
                )
                # Notify reviewer that new draft is ready
                await workflow.execute_activity(
                    notify_human_agent,
                    args=[article_id, regenerated_draft],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry,
                )
                # Loop back — wait for next decision on improved draft
                continue

            # ── Reject — discard draft entirely ──────────────
            elif action == "reject":
                await workflow.execute_activity(
                    reject_kb_article,
                    article_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry,
                )
                return {"status": "rejected", "article_id": article_id}

            # ── Unknown action — log and wait again ───────────
            else:
                continue
