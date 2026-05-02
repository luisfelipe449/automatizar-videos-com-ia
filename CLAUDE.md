# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an automated YouTube Shorts pipeline that transforms text prompts into published videos using free/low-cost APIs:

- **Groq** (Llama 3.3 for scripts, Whisper for transcription)
- **Gemini** (TTS, Imagen for images as fallback)
- **Pollinations.ai** (primary image generation, no API key required)
- **YouTube Data API v3** (OAuth2 upload)

The pipeline runs locally, renders with FFmpeg at 1080x1920 (9:16), and supports multi-account management with quota tracking.

## Architecture

**Data Flow**: `prompt → script → TTS → captions → visuals → render → upload`

**Execution Modes**:
1. **CLI Mode** (`app/orchestrator.py`): Direct pipeline execution for single videos
2. **Dashboard Mode** (`app/web/`): Web UI + background worker for multi-account queue management

**Key Components**:
- `app/orchestrator.py`: Pipeline orchestrator with idempotent stage execution
- `app/core/`: Job queue, account manager, pipeline runner (bridges queue and orchestrator)
- `app/services/`: One service per external API (script, TTS, captions, visuals, render, YouTube)
- `app/db/models.py`: SQLAlchemy models (Account, Job, Upload, QuotaSnapshot)
- `app/models.py`: Pydantic schemas - JSON contract between pipeline stages
- `app/worker.py`: Background job processor for dashboard mode
- `app/web/`: FastAPI dashboard (routes, templates)

**Pipeline Stages** (6 stages, idempotent with hash-based caching):
1. `script`: Generate Script from prompt via Groq/Bedrock LLM
2. `tts`: Synthesize narration audio via Gemini TTS → WAV
3. `captions`: Transcribe WAV via Groq Whisper → ASS format with karaoke timing
4. `clips`: Generate scene images (Pollinations primary, Imagen fallback), OR all-videos (fal.ai LTX Video when `USE_VIDEO=true`), OR hook-only video (fal.ai LTX Video for scene 1 only when `USE_VIDEO_HOOK=true`)
5. `render`: FFmpeg composition - Ken Burns zoompan on images, scale+crop on videos, burn captions, mux audio
6. `upload`: YouTube Data API v3 resumable upload with OAuth2 refresh token

## Development Commands

### Environment Setup
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash
# source .venv/bin/activate    # Linux/macOS

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with API keys
```

### Database
```bash
# Initialize database (creates data/app.db)
python scripts/init_db.py
```

### OAuth Bootstrap
```bash
# Generate YouTube refresh token (one-time setup per account)
python scripts/bootstrap_youtube_oauth.py --client-secrets path/to/client_secret.json
# OR if credentials already in .env:
python scripts/bootstrap_youtube_oauth.py
```

### CLI Mode
```bash
# Generate and upload a Short
python -m app.orchestrator --prompt "3 curiosidades sobre buracos negros"

# Render only (skip upload for local testing)
python -m app.orchestrator --prompt "test prompt" --skip-upload

# Override job_id (useful for cache management)
python -m app.orchestrator --prompt "test" --job-id my-custom-id
```

### Dashboard Mode
```bash
# Start web dashboard (terminal 1)
uvicorn app.web.main:app --reload --port 8000

# Start background worker (terminal 2)
python -m app.worker

# Dashboard will be at http://localhost:8000
# - /accounts: manage YouTube accounts
# - /jobs: create and monitor jobs
# - /dashboard: analytics (quota usage, upload stats)
```

### Testing
```bash
# Run all tests (unit only, fast)
pytest -v

# Include slow integration tests (requires FFmpeg)
pytest -v -m slow

# Exclude slow tests explicitly
pytest -v -m "not slow"

# Run specific test file
pytest -v tests/test_orchestrator.py

# Run specific test
pytest -v tests/test_models.py::test_script_validation
```

## Key Concepts

### Idempotency and Caching

Every pipeline stage is idempotent - artifacts are cached by hash of their input:
- Script: cached by prompt hash
- TTS: cached by narration text hash
- Captions: cached by narration text hash (protects against stale captions)
- Visuals: cached per scene by `image_prompt` hash
- Render: cached by mtime comparison (final.mp4 vs all inputs)
- Upload: cached by presence of `upload.json` with `video_id`

To force regeneration of a stage, delete both the artifact AND its `.hash` file from `assets/generated/<job_id>/`.

### Multi-Account System

**Account Model** (`app/db/models.py:Account`):
- Stores OAuth2 credentials per YouTube account
- Tracks `quota_used` / `daily_quota` (default 10,000 units/day)
- `quota_reset_at`: 24 hours from last reset
- `is_active`: allows disabling accounts without deletion

**Account Selection** (dashboard mode):
- User MUST select an account when creating a job (no auto-fallback)
- `PipelineRunner` validates account is active and has sufficient quota before starting
- Each upload consumes 1,600 quota units
- Quotas auto-reset 24h after last reset via worker loop

**Quota Management**:
- YouTube Data API `videos.insert` costs 1,600 units
- Daily limit: 10,000 units = ~6 uploads/day per account
- `AccountManager.consume_quota()`: increments `quota_used` after successful upload
- `AccountManager.reset_daily_quotas()`: worker calls this each iteration to reset expired quotas

### Job Queue

**Job States** (`app/db/models.py:Job`):
- `pending`: queued, waiting for worker
- `running`: worker processing
- `completed`: upload succeeded
- `failed`: error at any stage

**Job Lifecycle**:
1. User creates job via dashboard → `JobQueueManager.enqueue()` → status=`pending`
2. Worker polls → `JobQueueManager.get_next_pending()` → highest priority first
3. `PipelineRunner.run_job()` validates account, sets env vars, calls `orchestrator.run_pipeline()`
4. Pipeline stages update job status via `JobQueueManager.update_status()`
5. Success → `JobQueueManager.mark_completed()` creates `Upload` record
6. Failure → job remains with error message, can be retried via `retry_failed_job()`

### Pydantic Contracts

**`app/models.py`** defines the JSON schemas validated between stages:

- **Script**: Full LLM output with metadata + narration + scenes. Validates:
  - Total duration ≤ 58s (buffer for 60s Shorts limit)
  - Scene IDs are contiguous 1..N sequence
  - Description contains `#Shorts` hashtag
  - Tags are 1-30 chars, max 15 tags
  - Optional `rank` + `rank_title` for Top-N videos (both set or both null)

- **Scene**: One visual beat with `id`, `scene_text`, `image_prompt`, `duration_seconds`

- **RenderSpec**: Declarative input to `render_service` with all FFmpeg paths. `scene_paths` can be mixed `.png` (images) and `.mp4` (videos from fal.ai LTX Video)

### FFmpeg Rendering

**Image Scenes** (MVP default):
- Ken Burns effect via `zoompan` filter: `z='min(zoom+0.0015,1.2)'`
- Each image gets subtle slow zoom over its duration

**Video Scenes** (when `USE_VIDEO=true`):
- Scale to 1080x1920, center crop, freeze-pad if shorter than scene duration
- Fallback to PNG if fal.ai generation fails

**Caption Burning**:
- ASS format with karaoke timing (word-by-word highlighting)
- Font: Bebas Neue Regular from `assets/fonts/BebasNeue-Regular.ttf`
- Style: yellow (`#FFD700`) for current word, white for spoken words, black outline
- Loaded via `subtitles` filter with `fontsdir=assets/fonts`

**Final Output**:
- Codec: H.264 (libx264), preset medium, CRF 23
- Audio: AAC 128kbps
- Pixel format: yuv420p (broad compatibility)
- Flags: `+faststart` for web streaming

## Environment Variables

See `.env.example` for full documentation. Critical ones:

**Required for CLI mode**:
- `GROQ_API_KEY`: Groq API key for LLM + Whisper
- `GEMINI_API_KEY`: Google AI Studio key for TTS (and optional Imagen fallback)
- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`: OAuth2 for upload

**Optional**:
- `LLM_PROVIDER`: `groq` (default) or `bedrock` for AWS Claude
- `IMAGE_PROVIDER`: `pollinations` (default, free) or `imagen` (paid)
- `USE_VIDEO`: `true` to enable per-scene video clips via fal.ai LTX Video (requires `FAL_KEY`)
- `USE_VIDEO_HOOK`: `true` to make only scene 1 a video (cheaper than full `USE_VIDEO`). Requires `FAL_KEY`. Ignored when `USE_VIDEO=true`.
- `FFMPEG_PATH`: path to ffmpeg binary (empty = use PATH)
- `YOUTUBE_PRIVACY_STATUS`: `private` (default) | `unlisted` | `public`

## File Structure

```
assets/
  fonts/BebasNeue-Regular.ttf     # Caption font (loaded by libass)
  input/                          # Optional: prompt templates, background music
  generated/<job_id>/             # Per-job artifacts
    script.json                   # Validated Script (Pydantic)
    narration.wav                 # TTS output (24kHz 16-bit mono)
    captions.ass                  # ASS subtitles with karaoke timing
    scene_XX.png                  # Images from Pollinations/Imagen
    scene_XX.mp4                  # Videos from fal.ai (when USE_VIDEO=true)
    *.hash                        # Input hash for idempotency
    status.json                   # Per-stage status + timing
    upload.json                   # YouTube video_id after upload
  rendered/<job_id>/
    final.mp4                     # Final composed video

data/
  app.db                          # SQLite database (dashboard mode)

app/
  orchestrator.py                 # CLI entry point + pipeline flow control
  worker.py                       # Background job processor
  config.py                       # Env vars + constants
  models.py                       # Pydantic schemas (Script, Scene, RenderSpec)
  core/
    job_queue.py                  # Job CRUD: enqueue, get_next, update status
    account_manager.py            # Account CRUD + quota operations
    pipeline_runner.py            # Bridges queue and orchestrator
    analytics.py                  # Dashboard stats aggregation
  db/
    models.py                     # SQLAlchemy ORM (Account, Job, Upload, QuotaSnapshot)
    database.py                   # Session management
  services/
    script_service.py             # Groq Llama / Bedrock Claude
    tts_service.py                # Gemini TTS
    caption_service.py            # Groq Whisper → ASS format
    visual_service.py             # Pollinations + Imagen fallback
    video_service.py              # fal.ai LTX Video (optional)
    render_service.py             # FFmpeg composition
    youtube_service.py            # OAuth2 + resumable upload
  utils/
    filepaths.py                  # Deterministic path helpers
    cache.py                      # Hash-based artifact freshness checks
    retries.py                    # Tenacity retry decorators
    ffmpeg_helpers.py             # Subprocess wrappers + error parsing
    quota.py                      # Quota detection (429 → fallback)
  web/
    main.py                       # FastAPI app
    routes/                       # dashboard, jobs, accounts, api
    templates/                    # Jinja2 HTML

scripts/
  bootstrap_youtube_oauth.py      # One-time OAuth flow to get refresh_token
  init_db.py                      # Create database schema
  add_category_column.py          # Migration example

tests/
  test_*.py                       # Pytest suite (fast unit + slow integration)
```

## Common Workflows

### Adding a New Account
1. Get OAuth2 credentials from Google Cloud Console (see README.md "Multi-Account Setup")
2. Run `python scripts/bootstrap_youtube_oauth.py --client-secrets path/to/client_secret.json`
3. Dashboard → Accounts → Add Account → paste credentials
4. Account is validated and added to pool with `quota_used=0`, `quota_reset_at=now+24h`

### Handling Quota Exceeded
- If job fails with `"No available account with sufficient quota"`:
  1. Check `/accounts` page for quota status
  2. Wait for `quota_reset_at` (worker auto-resets)
  3. OR add more accounts
  4. Retry failed job via dashboard

### Manual Upload (when upload stage fails)
- If render completes but upload fails:
  1. Job page shows "Manual Upload Available" button
  2. Downloads `assets/rendered/<job_id>/final.mp4`
  3. User uploads manually to YouTube Studio

### Debugging Pipeline Failures
1. Check `assets/generated/<job_id>/status.json` for per-stage status + error
2. Inspect artifacts in `assets/generated/<job_id>/` to see which stage failed
3. Re-run with same `--job-id` to resume from last successful stage
4. Or delete specific artifact + hash to force re-execution of that stage

### Changing Fonts
1. Place new `.ttf` in `assets/fonts/`
2. Update `_ASS_FONTNAME` in `app/services/caption_service.py` to match font's internal name
3. Captions will use new font on next render

## Python Version

Project requires **Python 3.13+** (see `pyproject.toml`). Earlier versions may work but are untested.

## FFmpeg Requirements

FFmpeg 8+ must be in PATH or `FFMPEG_PATH` must point to the binary. Required filters:
- `zoompan` (Ken Burns on images)
- `scale`, `crop`, `tpad` (video scene processing)
- `subtitles` (ASS caption burning)
- `concat` (scene composition)

Install via package manager:
- Windows: `winget install Gyan.FFmpeg`
- macOS: `brew install ffmpeg`
- Debian/Ubuntu: `sudo apt install ffmpeg`

## Troubleshooting

### FFmpeg not found
- Verify: `ffmpeg -version`
- If not in PATH, set `FFMPEG_PATH=/path/to/ffmpeg` in `.env`

### OAuth token expired
- Symptom: `401 UNAUTHENTICATED` or `invalid_grant`
- Fix: Re-run `scripts/bootstrap_youtube_oauth.py` and update `.env` or account in dashboard

### Pollinations offline + no Imagen fallback
- Symptom: `VisualGenerationError ... no fallback available`
- Fix: Set `GEMINI_API_KEY` in `.env` to enable Imagen fallback

### Worker not processing jobs
- Check worker is running: `python -m app.worker` should print `[worker] Starting job processor...`
- Verify at least one account is active with quota
- Check worker terminal for errors

### Database corruption
- Backup `data/app.db`
- Re-run `python scripts/init_db.py` (WARNING: deletes all data)

## Code Style Notes

- **Type hints**: Used throughout (`from __future__ import annotations`)
- **Pathlib**: All file paths use `pathlib.Path`, not strings
- **Pydantic**: Validation happens at stage boundaries via `models.py` schemas
- **Tenacity**: Retry decorators in `utils/retries.py` for flaky APIs
- **SQLAlchemy**: ORM for dashboard mode, all timestamps use `timezone.utc`
- **Logging**: Print statements with `[prefix]` tags (no formal logging yet)

## Related Documentation

- `README.md`: User-facing setup guide, commands, troubleshooting
- `PLANO.md`: Original design document, architecture decisions
- `QUICK_OAUTH_SETUP.md`: Fast OAuth setup guide
- `docs/GCP_OAUTH_SETUP.md`: Detailed Google Cloud Console walkthrough
