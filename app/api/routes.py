from pydantic import BaseModel
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Security
from temporalio.client import Client
from app.workflows.kb_workflows import KBCreationWorkflow
from app.db.models import get_db, KBArticle, KBStatus
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.auth.auth import verify_api_key
import os
import uuid

router = APIRouter()
RequireAuth = Security(verify_api_key)


class ConversationMessage(BaseModel):
    role: str
    content: str
    timestamp: Optional[str] = None


class ResolvedTicket(BaseModel):
    ticket_id: str
    subject: str
    description: str
    conversation: list[ConversationMessage]
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None


class ReviewDecision(BaseModel):
    action: str  # "accept" | "accept_with_feedback" | "regenerate" | "reject"
    feedback: Optional[str] = None  # required for accept_with_feedback and regenerate


def article_to_helpdesk_format(article: KBArticle) -> dict:
    return {
        "id": article.id,
        "workflow_id": article.workflow_id,
        "title": article.title,
        "slug": article.slug,
        "content": article.content,
        "excerpt": article.excerpt,
        "categoryId": article.category_id,
        "isInternal": article.is_internal,
        "status": article.status.value if article.status else None,  # ← .value
        "ticket_id": article.ticket_id,
        "tags": article.tags.split(",") if article.tags else [],
        "similarity_score": article.similarity_score,
        "duplicate_of": article.duplicate_of,
        "reviewer_feedback": article.reviewer_feedback,
        "resolved_by": article.resolved_by,
        "created_at": article.created_at.isoformat() if article.created_at else None,
        "updated_at": article.updated_at.isoformat() if article.updated_at else None,
    }


async def get_temporal_client() -> Client:
    from temporalio.client import Client

    host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    api_key = os.getenv("TEMPORAL_API_KEY", "")

    # Temporal Cloud with API key
    if api_key:
        return await Client.connect(
            host,
            namespace=namespace,
            api_key=api_key,
            tls=True,  # required for Temporal Cloud
        )

    # Local Temporal (no auth, no TLS)
    return await Client.connect(host, namespace=namespace)


@router.post("/tickets/resolve", status_code=202, dependencies=[RequireAuth])
async def resolve_ticket(ticket: ResolvedTicket):
    client = await get_temporal_client()
    workflow_id = f"kb-{ticket.ticket_id}-{uuid.uuid4().hex[:8]}"
    await client.start_workflow(
        KBCreationWorkflow.run,
        ticket.model_dump(),
        id=workflow_id,
        task_queue="kb-task-queue",
    )
    return {
        "message": "KB creation workflow started",
        "workflow_id": workflow_id,
    }


@router.get("/kb-drafts", dependencies=[RequireAuth])
async def list_drafts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(KBArticle).where(KBArticle.status == KBStatus.pending_approval)
    )
    return [article_to_helpdesk_format(a) for a in result.scalars().all()]


@router.get("/kb-drafts/{article_id}", dependencies=[RequireAuth])
async def get_draft(article_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KBArticle).where(KBArticle.id == article_id))
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    return article_to_helpdesk_format(draft)


@router.patch("/kb-drafts/{article_id}/review", dependencies=[RequireAuth])
async def review_draft(
    article_id: str,
    body: ReviewDecision,
    db: AsyncSession = Depends(get_db),
):
    valid_actions = ("accept", "accept_with_feedback", "regenerate", "reject")
    if body.action not in valid_actions:
        raise HTTPException(
            status_code=422, detail=f"action must be one of: {', '.join(valid_actions)}"
        )

    # feedback required for accept_with_feedback and regenerate
    if body.action in ("accept_with_feedback", "regenerate") and not body.feedback:
        raise HTTPException(
            status_code=422, detail=f"feedback is required for action '{body.action}'"
        )

    result = await db.execute(select(KBArticle).where(KBArticle.id == article_id))
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    if not draft.workflow_id:
        raise HTTPException(status_code=400, detail="No workflow linked to this draft")

    client = await get_temporal_client()
    workflow_handle = client.get_workflow_handle(draft.workflow_id)

    # Send signal with action + feedback
    await workflow_handle.signal(
        KBCreationWorkflow.human_decision,
        {"action": body.action, "feedback": body.feedback or ""},
    )

    return {
        "message": f"Decision '{body.action}' sent to workflow",
        "article_id": article_id,
        "action": body.action,
        "feedback": body.feedback,
    }


@router.get("/kb", dependencies=[RequireAuth])
async def list_published_kb(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(KBArticle).where(KBArticle.status == KBStatus.approved)
    )
    articles = result.scalars().all()
    return [article_to_helpdesk_format(a) for a in articles]


@router.get("/kb/{article_id}", dependencies=[RequireAuth])
async def get_kb_article(article_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(KBArticle).where(
            KBArticle.id == article_id, KBArticle.status == KBStatus.approved
        )
    )
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article_to_helpdesk_format(article)
