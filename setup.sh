#!/usr/bin/env bash
set -e

echo "=================================================="
echo "CampaignForge Orchestrator Setup & Launch"
echo "=================================================="

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required but not installed. Please install Python 3.10+."
    exit 1
fi

echo "✓ Python 3 detected: $(python3 --version)"

# Check Groq API key
if [ -f ".env" ]; then
    source_groq_key=$(grep -E "^GROQ_API_KEY=" .env 2>/dev/null | cut -d'=' -f2- | tr -d '"'"'"' ' || echo "")
else
    source_groq_key=""
fi
ENV_GROQ="${GROQ_API_KEY:-$source_groq_key}"

if [ -z "$ENV_GROQ" ]; then
    echo "⚠️  WARNING: GROQ_API_KEY is not set."
    echo "    Copy .env.example to .env and add your Groq API key:"
    echo "    cp .env.example .env"
    echo "    Then set: GROQ_API_KEY=gsk_..."
    echo "    Get a free key at https://console.groq.com/"
else
    echo "✓ GROQ_API_KEY detected."
fi

# Create virtualenv if not present
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment 'venv'..."
    python3 -m venv venv
fi

echo "Activating virtualenv and installing dependencies..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# Ensure directories exist
mkdir -p data logs prompts config static

echo "=================================================="
echo "Starting CampaignForge Orchestrator on http://localhost:8000"
echo "Interactive Swagger API documentation available at http://localhost:8000/docs"
echo "=================================================="

./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
