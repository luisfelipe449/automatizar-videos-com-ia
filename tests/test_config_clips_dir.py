"""CLIPS_DIR: where clips mode writes its (large) files."""
from __future__ import annotations

import importlib
from pathlib import Path

import app.config as config


def _reload_with(monkeypatch, value: str | None):
    if value is None:
        monkeypatch.delenv("CLIPS_DIR", raising=False)
    else:
        monkeypatch.setenv("CLIPS_DIR", value)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    return importlib.reload(config)


def test_clips_dir_defaults_to_assets(monkeypatch):
    try:
        assert _reload_with(monkeypatch, "").CLIPS_DIR == config.ASSETS_DIR / "clips"
        assert _reload_with(monkeypatch, None).CLIPS_DIR == config.ASSETS_DIR / "clips"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_clips_dir_from_env(monkeypatch, tmp_path):
    try:
        assert _reload_with(monkeypatch, f"  {tmp_path}  ").CLIPS_DIR == Path(str(tmp_path))
    finally:
        monkeypatch.undo()
        importlib.reload(config)
