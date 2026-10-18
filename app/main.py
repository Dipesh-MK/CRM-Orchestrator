import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_config
from app.core.storage import get_storage
from app.core.llm_client import get_llm_client
from app.api.chat_routes import router as chat_router
from app.api.logs_routes import router as logs_router
from app.api.dispatch_routes import router as dispatch_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure storage and health check
    storage = get_storage()
    llm = get_llm_client()
    health = await llm.check_health()
    print("==================================================")
    print("🚀 CampaignForge Orchestrator Starting Up")
    print(f"LLM Base URL: {llm.config.llm.base_url}")
    print(f"Target Model: {llm.config.llm.model}")
    print(f"Health Status: {health.get('message')}")
    if not health.get("reachable"):
        print("⚠️ WARNING: LLM endpoint unreachable! Check Ollama / API.")
    print("==================================================")
    yield

app = FastAPI(
    title="CampaignForge Orchestrator",
    description="Agentic CRM campaign planning and message template orchestrator for any business.",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(chat_router)
app.include_router(logs_router)
app.include_router(dispatch_router)

# Health endpoint
@app.get("/api/health")
async def health_check():
    llm = get_llm_client()
    health = await llm.check_health()
    cfg = get_config()
    return {
        "status": "healthy" if health.get("reachable") else "degraded",
        "llm_health": health,
        "config": {
            "model": cfg.llm.model,
            "base_url": cfg.llm.base_url,
            "word_limit": cfg.orchestrator.word_limit,
            "max_retries": cfg.orchestrator.max_retries,
            "optout_text": cfg.orchestrator.optout_text
        }
    }

# Serve static UI
static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def serve_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "CampaignForge backend is running. static/index.html not found."}
