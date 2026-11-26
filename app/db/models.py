from sqlalchemy import Column, String, Text, DateTime, Enum, Float, Boolean
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime
import enum, os, re

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class KBStatus(str, enum.Enum):
    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"
    regenerating = "regenerating"


class KBArticle(Base):
    __tablename__ = "kb_articles"

    id = Column(String, primary_key=True)
    ticket_id = Column(String, nullable=False, index=True)

    # Helpdesk schema fields
    title = Column(String, nullable=False)
    slug = Column(String, nullable=False)
    content = Column(Text, nullable=False)  # markdown formatted
    excerpt = Column(Text, nullable=True)
    category_id = Column(String, nullable=True)
    is_internal = Column(Boolean, default=True)

    # Internal tracking fields
    problem = Column(Text, nullable=False)
    tags = Column(String, nullable=True)
    status = Column(Enum(KBStatus), default=KBStatus.draft)
    similarity_score = Column(Float, nullable=True)
    duplicate_of = Column(String, nullable=True)
    workflow_id = Column(String, nullable=True)
    reviewer_feedback = Column(Text, nullable=True)  # feedback from human reviewer
    resolved_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    slug = re.sub(r"^-+|-+$", "", slug)
    return slug


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
