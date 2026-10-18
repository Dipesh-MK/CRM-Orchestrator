# CampaignForge Orchestrator

CampaignForge Orchestrator is a mock agentic CRM messaging orchestrator that assists **any business or organization** (retail, SaaS, healthcare clinics, educational institutions, NGOs, etc.) plan and generate compliant CRM messaging sequences.

---

## Key Features & Architecture

- **Zero Hardcoding**:
  - No hardcoded tones, question lists, domain use cases, org names, campaign fields, word limits, or opt-out text in code.
  - All prompts are editable markdown files in `/prompts` loaded dynamically at runtime.
  - All configurations live in `config/app.yaml` with `.env` overrides.
- **Adaptive Discovery with Persistent Memory**:
  - Identifies domain and dynamically creates slot schemas.
  - Asks 1-2 focused questions at a time.
  - Retains durable facts and tone preferences across sessions in SQLite workspace memory.
- **Tone Exploration**:
  - Dynamically proposes 3-5 tailored tones with authentic message previews using the campaign's real details.
  - User can select, regenerate with feedback, or suggest a tone.
- **Dynamic Orchestrator Planning**:
  - Dynamically builds custom pipeline steps tailored to the business domain. Emits standard `<pipeline_event>` milestones.
- **Compliance & Template Generation**:
  - Enforces word limit ceiling (default 320 words).
  - Automatically regenerates and repairs templates that exceed limits or lack mandatory opt-out text.
  - Generates 3-row recipient tag previews (with name and masked phone).
- **Code-Level Safety Guard**:
  - Strictly classifies requests against `prompts/safety_policy.md` before processing and before rendering templates. Fails closed on violations.
- **Comprehensive Logging & Transparency**:
  - SQLite database (`data/campaignforge.db`) + append-only JSONL mirrors in `/logs`.
  - Captures full prompts, raw responses, latencies, tokens, validations, and memory operations.

---

## Quickstart Setup

### Prerequisites
1. Python 3.10+
2. Ollama installed (or any OpenAI-compatible API)

### 1. Run Setup Script
```bash
./setup.sh
```
This script will:
- Check for Python and Ollama.
- Pull `qwen2.5:7b-instruct` (or inform if another model is desired).
- Create a virtual environment and install dependencies from `requirements.txt`.
- Launch the FastAPI server on `http://localhost:8000`.

### 2. Manual Run
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` in your browser.
API documentation is available at `http://localhost:8000/docs`.

---

## Configuration

Edit `config/app.yaml` or set environment variables in `.env`:
```yaml
llm:
  base_url: "http://localhost:11434/v1"
  model: "qwen2.5:7b-instruct"
  api_key: "ollama"
  temperature: 0.3

orchestrator:
  word_limit: 320
  max_retries: 3
  max_discovery_rounds: 5
  optout_text: "Reply STOP to unsubscribe."
```

---

## Assumptions & Design Choices

1. **Local Ollama Integration**: Defaults to `http://localhost:11434/v1` and `qwen2.5:7b-instruct`. If local model is unreachable, the startup health check warns immediately and gracefully logs the status.
2. **Pluggable Input Adapters**: Supports CSV, TSV, XLSX, and pasted lines format (`app/adapters/input_adapter.py`).
3. **Deterministic Pipeline Events**: Backend orchestrator code deterministically emits `<pipeline_event>` structured events via SSE.
4. **Safety Guard**: Operates on a fail-closed paradigm. If the safety check fails to validate safely after retries, the campaign request is blocked.

For detailed logging instructions, see [LOGGING_GUIDE.md](file:///home/aracknab/Projects/CRM%20Orchaestrator/LOGGING_GUIDE.md).
