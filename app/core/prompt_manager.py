import os
from pathlib import Path
from typing import Dict, Optional
from app.core.config import get_config

class PromptManager:
    def __init__(self, prompts_dir: Optional[str] = None):
        cfg = get_config()
        self.prompts_dir = Path(prompts_dir or cfg.paths.prompts_dir)

    def load_prompt(self, name: str) -> str:
        if not name.endswith(".md"):
            name = f"{name}.md"
        path = self.prompts_dir / name
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def render(self, name: str, variables: Optional[Dict[str, str]] = None) -> str:
        template = self.load_prompt(name)
        if variables:
            for k, v in variables.items():
                template = template.replace(f"{{{{{k}}}}}", str(v))
        return template

_prompt_manager: Optional[PromptManager] = None

def get_prompt_manager() -> PromptManager:
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager()
    return _prompt_manager
