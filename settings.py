"""
Settings = default_config.json, overlaid with config.json (if present).

config.json is what the "Save settings" button writes; it only needs to hold
the values you changed. Secrets (the SSA password) are never written here –
they come from the SSA_PASSWORD environment variable, .streamlit/secrets.toml,
or the password box in the sidebar.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULTS_PATH = HERE / "default_config.json"
USER_PATH = HERE / "config.json"


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        # *_overrides are free-form dicts: replace them, don't merge
        if isinstance(value, dict) and isinstance(out.get(key), dict) and not key.endswith("overrides"):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_defaults() -> dict:
    return json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))


def load_config() -> dict:
    cfg = load_defaults()
    if USER_PATH.exists():
        cfg = deep_merge(cfg, json.loads(USER_PATH.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg: dict, path: Path = USER_PATH) -> None:
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
