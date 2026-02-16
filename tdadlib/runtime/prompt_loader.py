from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import yaml


def load_tool_description_overrides(agent_dir: str | Path) -> dict[str, str]:
    """Load tool description overrides from agent artifacts.

    Tool descriptions heavily influence when/how an agent chooses to call tools.
    This allows PromptSmith to optimize tool descriptions alongside the system prompt.

    If TDAD_TOOL_DESC_OVERRIDE_PATH is set, loads from that path instead.
    This allows mutation testing to inject mutant tool descriptions.

    Returns:
        Dict mapping tool_name -> description override.
        Empty dict if no overrides file exists.
    """
    # Check for tool description override (used by mutation testing)
    env_override_path = os.environ.get("TDAD_TOOL_DESC_OVERRIDE_PATH")
    if env_override_path:
        override_path = Path(env_override_path)
        if override_path.exists():
            data = yaml.safe_load(override_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        return {}

    p = Path(agent_dir)
    override_path = p / "tool_descriptions.yaml"
    if override_path.exists():
        data = yaml.safe_load(override_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    return {}


def load_prompt_and_config(agent_dir: str | Path) -> tuple[str, dict]:
    """Load system prompt and config from agent directory.

    If TDAD_PROMPT_OVERRIDE_PATH is set, loads prompt from that path instead.
    This allows mutation testing to inject mutant prompts without modifying
    the original prompt file.
    """
    p = Path(agent_dir)
    cfg_path = p / "agent_config.yaml"

    # Check for prompt override (used by mutation testing)
    override_path = os.environ.get("TDAD_PROMPT_OVERRIDE_PATH")
    if override_path:
        prompt_path = Path(override_path)
    else:
        prompt_path = p / "system_prompt.txt"

    system_prompt = prompt_path.read_text(encoding="utf-8")

    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return system_prompt, cfg
