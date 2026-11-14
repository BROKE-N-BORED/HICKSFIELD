"""Where HicksField keeps its files.

User data lives outside the install folder so an update never wipes it:
- data: ~/.local/share/hicksfield (models, engines, logs)
- config: ~/.config/hicksfield
- projects: ~/HicksField (plain folders, one per film)

Tests redirect these with HICKSFIELD_DATA_DIR, HICKSFIELD_CONFIG_DIR, and
HICKSFIELD_HOME.
"""

import os
from pathlib import Path

APP_DIR_NAME = "hicksfield"
PROJECTS_DIR_NAME = "HicksField"


def _home():
    override = os.environ.get("HICKSFIELD_HOME")
    if override:
        return Path(override)
    return Path.home()


def data_dir():
    override = os.environ.get("HICKSFIELD_DATA_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else _home() / ".local" / "share"
    return base / APP_DIR_NAME


def config_dir():
    override = os.environ.get("HICKSFIELD_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else _home() / ".config"
    return base / APP_DIR_NAME


def log_dir():
    return data_dir() / "logs"


def models_dir():
    return data_dir() / "models"


def engines_dir():
    return data_dir() / "engines"


def voices_dir():
    return models_dir() / "voices"


def projects_dir():
    return _home() / PROJECTS_DIR_NAME


def ensure_dirs():
    """Create the app folders. Safe to call on every start."""
    for folder in (data_dir(), config_dir(), log_dir(), models_dir(),
                   engines_dir(), voices_dir(), projects_dir()):
        folder.mkdir(parents=True, exist_ok=True)
    return {
        "data": data_dir(),
        "config": config_dir(),
        "logs": log_dir(),
        "models": models_dir(),
        "engines": engines_dir(),
        "voices": voices_dir(),
        "projects": projects_dir(),
    }
