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

# Check Ollama
if command -v ollama &> /dev/null; then
    echo "✓ Ollama CLI detected."
    # Check if Ollama service is running
    if curl -s http://localhost:11434/api/tags &> /dev/null; then
        echo "✓ Local Ollama service is running."
    else
        echo "ℹ️ Starting Ollama service in background or run 'ollama serve' in another terminal."
    fi

    # Check/Pull target model
    echo "Checking for default target model: qwen2.5:7b-instruct..."
    if ollama list | grep -q "qwen2.5:7b-instruct"; then
        echo "✓ Model qwen2.5:7b-instruct is already installed."
    else
        echo "Pulling model qwen2.5:7b-instruct via Ollama (this may take a few minutes)..."
        ollama pull qwen2.5:7b-instruct || echo "⚠️ Could not pull qwen2.5:7b-instruct right now. You can pull it manually or use another model in .env"
    fi
else
    echo "⚠️ Ollama not detected in PATH. You can install it from https://ollama.ai or configure a hosted OpenAI-compatible model in .env"
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
