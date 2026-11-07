import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_config, load_env_defaults
load_env_defaults()
from app.core.storage import get_storage
from app.core.llm_client import get_llm_client
import yaml
from app.api.chat_routes import router as chat_router
from app.api.logs_routes import router as logs_router
from app.api.dispatch_routes import router as dispatch_router
from app.api.phone_routes import router as phone_router
from app.api.webhook_routes import router as webhook_router
from app.api.tool_webhook_routes import router as tool_webhook_router
from app.api.observatory_routes import router as observatory_router

def validate_models_independence():
    """Validates that Agent 1, Agent 2, and Guardrail Agent have independent model IDs and providers."""
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "models.yaml")
    if not os.path.exists(cfg_path):
        return
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    agents = data.get("agents", {})
    a1 = agents.get("agent1_drafter", {})
    a2 = agents.get("agent2_checker", {})
    gr = agents.get("guardrail_agent", {}).get("primary", {})
    
    m1 = a1.get("model")
    m2 = a2.get("model")
    m3 = gr.get("model")
    
    p1 = a1.get("provider")
    p3 = gr.get("provider")
    
    if m1 and m2 and m3:
        if len({m1, m2, m3}) < 3:
            raise ValueError(f"Model Independence Violation: Agent 1 ({m1}), Agent 2 ({m2}), and Guardrail ({m3}) must have 3 distinct model IDs.")
    if p1 and p3 and p1 == p3:
        print(f"⚠️ WARNING: Guardrail Agent provider '{p3}' is identical to Agent 1 provider '{p1}'. Independent providers are strongly recommended.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure storage, health check, and models independence validation
    validate_models_independence()
    storage = get_storage()
    llm = get_llm_client()
    health = await llm.check_health()
    print("==================================================")
    print("🚀 CampaignForge Orchestrator Starting Up (v2 Three-Agent)")
    print(f"LLM Base URL: {llm.config.llm.base_url}")
    print(f"Target Model: {llm.config.llm.model}")
    print(f"Health Status: {health.get('message')}")
    if not health.get("reachable"):
        print("⚠️ WARNING: LLM endpoint unreachable! Check Ollama / API.")
    print("Agent Observatory: http://localhost:8080/observatory")
    print("==================================================")
    yield

app = FastAPI(
    title="CampaignForge Orchestrator",
    description="Agentic CRM campaign planning and message template orchestrator for any business.",
    version="2.0.0",
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
app.include_router(phone_router)
app.include_router(webhook_router)
app.include_router(tool_webhook_router)
app.include_router(observatory_router)

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

@app.get("/observatory")
def serve_observatory():
    obs_file = os.path.join(static_dir, "observatory.html")
    if os.path.exists(obs_file):
        return FileResponse(obs_file)
    return {"message": "static/observatory.html not found."}

