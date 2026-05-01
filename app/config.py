"""Configuration: environment variables and pipeline-wide constants.

Design notes:
- Env vars are loaded lazily. Services pull what they need via `require(...)`.
- Only filesystem paths are eagerly resolved at import time.
- Paths use `pathlib.Path` throughout to stay portable across Windows / POSIX.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Path constants (always resolved relative to repo root, absolute).
# This file lives at <repo>/app/config.py, so BASE_DIR = parent.parent.
# -----------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent.parent
ASSETS_DIR: Path = BASE_DIR / "assets"
INPUT_DIR: Path = ASSETS_DIR / "input"
GENERATED_DIR: Path = ASSETS_DIR / "generated"
RENDERED_DIR: Path = ASSETS_DIR / "rendered"

# Load .env from repo root (no-op if file is missing).
load_dotenv(BASE_DIR / ".env")

# -----------------------------------------------------------------------------
# Credentials (lazy — may be empty strings until a service calls `require`).
# -----------------------------------------------------------------------------
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
YOUTUBE_CLIENT_ID: str = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET: str = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN: str = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

# -----------------------------------------------------------------------------
# LLM provider selection for script generation.
# -----------------------------------------------------------------------------
# One of: "groq" | "bedrock". Default = groq (free tier, Llama 3.3).
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "groq").strip().lower()

# -----------------------------------------------------------------------------
# Image provider selection for scene generation.
# -----------------------------------------------------------------------------
# One of: "pollinations" | "imagen". Default = pollinations (free, no key).
# The OTHER provider is the fallback when the primary fails. Imagen requires
# a paid Gemini plan + GEMINI_API_KEY.
IMAGE_PROVIDER: str = os.getenv("IMAGE_PROVIDER", "pollinations").strip().lower()

# -----------------------------------------------------------------------------
# Optional per-scene video mode.
# -----------------------------------------------------------------------------
# When USE_VIDEO is truthy, the visuals stage generates 5s MP4 clips via
# fal.ai LTX Video instead of still PNGs. Default off so the MVP pipeline
# (no fal.ai credentials, no paid API) keeps working out of the box.
# FAL_KEY is read by fal_client directly from the environment.
USE_VIDEO: bool = os.getenv("USE_VIDEO", "false").strip().lower() in ("true", "1", "yes")

# -----------------------------------------------------------------------------
# Model names (overridable via env).
# -----------------------------------------------------------------------------
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Bedrock uses inference profile IDs; us-east-1 requires the "us." prefix.
BEDROCK_MODEL: str = os.getenv(
    "BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
GEMINI_TTS_MODEL: str = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
GEMINI_IMAGE_MODEL: str = os.getenv("GEMINI_IMAGE_MODEL", "imagen-4.0-generate-001")

# -----------------------------------------------------------------------------
# Tooling / upload.
# -----------------------------------------------------------------------------
# Absolute path to ffmpeg binary. Empty string means "look up on PATH".
FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "")

# One of: private | unlisted | public. Default = private (MVP safe).
YOUTUBE_PRIVACY_STATUS: str = os.getenv("YOUTUBE_PRIVACY_STATUS", "private")

# -----------------------------------------------------------------------------
# Pipeline constants.
# -----------------------------------------------------------------------------
# Shorts must be <60s; 58s leaves a 2s buffer for fade-out.
MAX_DURATION_SECONDS: int = 58
TARGET_WIDTH: int = 1080
TARGET_HEIGHT: int = 1920


class MissingEnvVar(RuntimeError):
    """Raised when a required environment variable is missing or empty."""


def require(var_name: str) -> str:
    """Return the value of a required env var, or raise a clear error.

    Services call this lazily so importing `app.config` never fails even
    when some credentials are not yet configured.
    """
    value = os.getenv(var_name, "").strip()
    if not value:
        raise MissingEnvVar(
            f"Environment variable '{var_name}' is required but not set. "
            f"Add it to your .env (see .env.example)."
        )
    return value
