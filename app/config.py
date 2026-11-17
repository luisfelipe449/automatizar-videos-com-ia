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
# Optional hook-only video mode.
# -----------------------------------------------------------------------------
# When USE_VIDEO_HOOK is truthy AND USE_VIDEO is falsy, the visuals stage
# generates a single MP4 for scene 1 (via fal.ai LTX Video) while scenes
# 2..N stay as PNGs. Captures the "hook" retention boost of motion without
# paying for a full-video Short. Ignored when USE_VIDEO=true (USE_VIDEO
# wins — all scenes become video).
USE_VIDEO_HOOK: bool = os.getenv("USE_VIDEO_HOOK", "false").strip().lower() in ("true", "1", "yes")

# -----------------------------------------------------------------------------
# Model names (overridable via env).
# -----------------------------------------------------------------------------
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Completion budget for script generation. Reasoning models (openai/gpt-oss-*)
# spend this on hidden reasoning before emitting any JSON, so they need far more
# headroom than a plain instruct model. Note Groq counts this value toward the
# per-minute token limit, so raising it can trip a 413 on the free tier.
GROQ_MAX_TOKENS: int = int(os.getenv("GROQ_MAX_TOKENS", "4096"))
# "low" | "medium" | "high" for reasoning models; empty means don't send the
# parameter at all — Groq returns 400 for models that don't support it.
GROQ_REASONING_EFFORT: str = os.getenv("GROQ_REASONING_EFFORT", "").strip()
# Bedrock uses inference profile IDs; us-east-1 requires the "us." prefix.
BEDROCK_MODEL: str = os.getenv(
    "BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
GEMINI_TTS_MODEL: str = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
# Gemini prebuilt voice used for narration. The voices are multilingual, but
# they don't all sound equally natural per language — switching narration
# language usually means auditioning a different voice.
TTS_VOICE: str = os.getenv("TTS_VOICE", "Kore")
GEMINI_IMAGE_MODEL: str = os.getenv("GEMINI_IMAGE_MODEL", "imagen-4.0-generate-001")

# -----------------------------------------------------------------------------
# Tooling / upload.
# -----------------------------------------------------------------------------
# Absolute path to ffmpeg binary. Empty string means "look up on PATH".
FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "")

# One of: private | unlisted | public. Default = private (MVP safe).
YOUTUBE_PRIVACY_STATUS: str = os.getenv("YOUTUBE_PRIVACY_STATUS", "private")

# BCP-47 tag written to the video's `defaultLanguage` / `defaultAudioLanguage`.
# YouTube feeds these to auto-captions and recommendations, so a pt-BR Short
# tagged "en" gets matched to the wrong audience. Falls back to the narration
# language already configured for Whisper captions, so switching a channel's
# language needs one variable, not two. BCP-47 accepts a region subtag that
# ISO-639-1 doesn't, hence the explicit override: WHISPER_LANGUAGE must stay
# `pt` while this can be the more precise `pt-BR`.
YOUTUBE_VIDEO_LANGUAGE: str = (
    os.getenv("YOUTUBE_VIDEO_LANGUAGE", "").strip()
    or os.getenv("WHISPER_LANGUAGE", "").strip()
    or "en"
)

# -----------------------------------------------------------------------------
# Clips mode (app/clips). Twitch credentials are read lazily via `require`.
# -----------------------------------------------------------------------------
# Downloaded clips, outros and rendered editions: several GB a day, so it can
# live on another drive. A path for the Python that runs the app (on this
# setup Windows Python, so "F:/five-star-clips").
CLIPS_DIR: Path = Path(os.getenv("CLIPS_DIR", "").strip() or ASSETS_DIR / "clips")
CLIPS_GAME_NAME: str = os.getenv("CLIPS_GAME_NAME", "Grand Theft Auto VI").strip()
# Clips under this view count are dropped before download/classification —
# the classifier is the only per-clip cost, so this is the budget knob.
CLIPS_MIN_VIEWS: int = int(os.getenv("CLIPS_MIN_VIEWS", "50"))
CLIPS_MAX_CANDIDATES: int = int(os.getenv("CLIPS_MAX_CANDIDATES", "60"))
# The channels speak these languages (ISO 639-1, from the stream's language).
# Clips in any other language rank with their score times the factor: at 0.5
# a Spanish clip needs twice the views of an English one to place above it.
# 1.0 turns the preference off.
CLIPS_PREFERRED_LANGUAGES: tuple[str, ...] = tuple(
    lang.strip().lower()
    for lang in os.getenv("CLIPS_PREFERRED_LANGUAGES", "en,pt").split(",")
    if lang.strip()
)
CLIPS_OTHER_LANGUAGE_FACTOR: float = float(os.getenv("CLIPS_OTHER_LANGUAGE_FACTOR", "0.5"))
# Gemini models tried in order for each clip; an overloaded (503/504) model
# hands the clip to the next. Lite comes last: cheaper and less contended,
# but a weaker judge of humour.
CLIPS_CLASSIFIER_MODELS: list[str] = [
    m.strip()
    for m in os.getenv("CLIPS_CLASSIFIER_MODELS", "gemini-flash-latest,gemini-flash-lite-latest").split(",")
    if m.strip()
]

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
