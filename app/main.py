from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db.models import init_db

    await init_db()
    yield


app = FastAPI(
    title="KB Creator Agent API",
    description="AI-powered KB creation from resolved support tickets",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api.routes import router

app.include_router(router, prefix="/api/v1")


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}
