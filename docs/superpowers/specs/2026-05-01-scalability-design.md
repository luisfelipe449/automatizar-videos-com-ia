# Scalability Design - Multi-Account YouTube Automation System

**Date:** 2026-05-01  
**Status:** Approved for implementation  
**Target:** 10-30 videos/day (2-5 YouTube accounts)

---

## 1. Executive Summary

This design upgrades the existing MVP (single-account, CLI-only pipeline) to a scalable system supporting multiple YouTube accounts with a web dashboard. The approach balances implementation speed (~7 days) with architectural quality, using FastAPI + SQLite + gradual migration to avoid breaking existing functionality.

**Key improvements:**
- Multi-account management with automatic quota tracking
- Web dashboard for job submission, monitoring, and analytics
- Job queue system with priority support
- Automated account selection based on available quota
- Full backward compatibility with existing CLI workflow

---

## 2. Architecture Overview

### 2.1 System Layers

```
┌─────────────────────────────────────────┐
│  Interfaces (2 entry points)            │
│  ├─ CLI (orchestrator.py - existing)    │
│  └─ Web Dashboard (FastAPI app)         │
└─────────────────────────────────────────┘
           ↓
┌─────────────────────────────────────────┐
│  Core Services (isolated)                │
│  ├─ Account Manager (CRUD accounts)      │
│  ├─ Job Queue Manager (queue + dispatch)│
│  ├─ Pipeline Runner (orchestrates)       │
│  └─ Analytics Aggregator                 │
└─────────────────────────────────────────┘
           ↓
┌─────────────────────────────────────────┐
│  Data Layer (SQLAlchemy + SQLite)        │
│  └─ models: Account, Job, Upload, Quota │
└─────────────────────────────────────────┘
           ↓
┌─────────────────────────────────────────┐
│  Existing Pipeline Services (unchanged)  │
│  script→tts→captions→visual→render→youtube│
└─────────────────────────────────────────┘
```

**Design principles:**
- **Backward compatible**: Existing CLI continues working without modifications
- **DB as intelligent cache**: Files (JSON, MP4) remain source of truth for assets; DB stores metadata
- **Separation of concerns**: `orchestrator.py` doesn't need to know about DB; `PipelineRunner` bridges the gap
- **No breaking changes**: All new code in separate namespaces (`db/`, `core/`, `web/`)

### 2.2 Directory Structure

```
automatizar-videos-com-ia/
├── app/
│   ├── config.py                    # (existing - no changes)
│   ├── models.py                    # (existing - Pydantic schemas)
│   ├── orchestrator.py              # (refactored - exports run_pipeline)
│   │
│   ├── services/                    # (existing - no changes)
│   │   ├── script_service.py
│   │   ├── tts_service.py
│   │   ├── caption_service.py
│   │   ├── visual_service.py
│   │   ├── render_service.py
│   │   └── youtube_service.py
│   │
│   ├── utils/                       # (existing - no changes)
│   │   ├── cache.py
│   │   ├── filepaths.py
│   │   ├── retries.py
│   │   ├── ffmpeg_helpers.py
│   │   └── quota.py
│   │
│   ├── db/                          # NEW
│   │   ├── __init__.py
│   │   ├── database.py              # engine + session factory
│   │   └── models.py                # SQLAlchemy tables
│   │
│   ├── core/                        # NEW
│   │   ├── __init__.py
│   │   ├── account_manager.py
│   │   ├── job_queue.py
│   │   ├── pipeline_runner.py
│   │   └── analytics.py
│   │
│   ├── web/                         # NEW
│   │   ├── __init__.py
│   │   ├── main.py                  # FastAPI app factory
│   │   ├── dependencies.py          # DB session dependency
│   │   ├── routes/
│   │   │   ├── dashboard.py
│   │   │   ├── accounts.py
│   │   │   ├── jobs.py
│   │   │   └── api.py
│   │   ├── templates/
│   │   │   ├── base.html
│   │   │   ├── dashboard.html
│   │   │   ├── accounts.html
│   │   │   ├── account_form.html
│   │   │   ├── jobs.html
│   │   │   └── job_detail.html
│   │   └── static/
│   │       ├── css/
│   │       └── js/
│   │
│   └── worker.py                    # NEW - job processor
│
├── scripts/
│   ├── bootstrap_youtube_oauth.py   # (existing - no changes)
│   └── init_db.py                   # NEW - creates SQLite tables
│
├── data/                            # NEW
│   └── app.db                       # SQLite database
│
├── requirements.txt                 # updated
└── .env.example                     # updated
```

---

## 3. Database Schema

### 3.1 Tables

#### `accounts`
Stores YouTube account credentials and quota state.

```sql
CREATE TABLE accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,          -- "Main Account", "Account 2"
    email           TEXT NOT NULL,                 -- Google account email
    client_id       TEXT NOT NULL,
    client_secret   TEXT NOT NULL,
    refresh_token   TEXT NOT NULL,                 -- OAuth refresh token
    daily_quota     INTEGER DEFAULT 10000,         -- YouTube API quota limit
    quota_used      INTEGER DEFAULT 0,             -- current usage (reset daily)
    quota_reset_at  TIMESTAMP NOT NULL,            -- next reset time (00:00 PT)
    is_active       BOOLEAN DEFAULT TRUE,          -- can receive new jobs?
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Why:** Centralizes multi-account credentials; tracks quota in real-time; enables automatic account selection.

#### `jobs`
Job queue and execution history.

```sql
CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT UNIQUE NOT NULL,          -- "2026-05-01_topic-slug_abc123"
    account_id      INTEGER,                       -- FK to accounts.id (NULL = not assigned)
    prompt          TEXT NOT NULL,                 -- user input
    status          TEXT NOT NULL,                 -- pending/running/completed/failed
    stage           TEXT,                          -- script/tts/captions/visual/render/upload
    error_message   TEXT,                          -- populated on failure
    priority        INTEGER DEFAULT 0,             -- higher = executes first
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
```

**Why:** Enables queue management, retry logic, progress tracking, and historical analysis.

#### `uploads`
YouTube upload records for analytics.

```sql
CREATE TABLE uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,                 -- FK to jobs.job_id
    account_id      INTEGER NOT NULL,              -- FK to accounts.id
    video_id        TEXT NOT NULL,                 -- YouTube video ID
    title           TEXT NOT NULL,
    privacy_status  TEXT NOT NULL,                 -- private/unlisted/public
    views           INTEGER DEFAULT 0,             -- synced periodically
    uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
```

**Why:** Separates upload metadata from job execution for analytics queries; enables view tracking.

#### `quota_snapshots`
Daily quota usage history for trend analysis.

```sql
CREATE TABLE quota_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL,              -- FK to accounts.id
    date            DATE NOT NULL,                 -- snapshot date
    quota_used      INTEGER NOT NULL,              -- usage at end of day
    uploads_count   INTEGER NOT NULL,              -- videos uploaded that day
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    UNIQUE (account_id, date)
);
```

**Why:** Historical data for charting quota trends; helps predict when quota increases are needed.

### 3.2 Relationships

- `jobs.account_id` → `accounts.id`: Which account executed/will execute the job
- `uploads.account_id` → `accounts.id`: Which account uploaded the video
- `uploads.job_id` → `jobs.job_id`: Which job generated the video
- `quota_snapshots.account_id` → `accounts.id`: Daily usage per account

---

## 4. Core Business Logic Components

### 4.1 AccountManager (`app/core/account_manager.py`)

**Responsibilities:**
- CRUD operations for YouTube accounts
- Validate OAuth refresh tokens (calls `youtube_service.verify_credentials`)
- Track and update quota usage
- Select next available account for job assignment
- Reset quotas daily (YouTube API resets at 00:00 Pacific Time)

**Key methods:**
```python
def add_account(name: str, email: str, client_id: str, 
                client_secret: str, refresh_token: str) -> Account:
    """Add new account, validate credentials before saving."""

def get_available_account() -> Account | None:
    """Return account with most available quota (quota_used + 1600 <= daily_quota 
    and is_active=True). Returns None if all exhausted."""

def consume_quota(account_id: int, amount: int = 1600) -> None:
    """Increment quota_used after successful upload. 1600 = videos.insert cost."""

def reset_daily_quotas() -> None:
    """Reset quota_used=0 for accounts where now >= quota_reset_at, 
    set next reset time (+24h)."""

def list_accounts(active_only: bool = False) -> list[Account]:
    """List all accounts, optionally filtered by is_active."""

def toggle_active(account_id: int) -> Account:
    """Toggle is_active flag (manual disable/enable)."""
```

**Error handling:**
- Invalid refresh token → mark `is_active=False`, set `error_message`
- Quota exhausted → skip in `get_available_account()` until reset

**Why this design:** Centralizes account logic; keeps YouTube API details abstracted; enables easy testing with mock accounts.

### 4.2 JobQueueManager (`app/core/job_queue.py`)

**Responsibilities:**
- Enqueue new jobs (status=pending)
- Retrieve next pending job (respects priority, then FIFO)
- Update job status/stage as pipeline progresses
- Support manual retry of failed jobs

**Key methods:**
```python
def enqueue(prompt: str, priority: int = 0, 
            account_id: int | None = None) -> Job:
    """Create new job. If account_id is None, will be auto-assigned by worker."""

def get_next_pending() -> Job | None:
    """Return highest priority pending job where account_id is NULL or assigned 
    to an active account. Returns None if queue empty."""

def update_status(job_id: str, status: str, stage: str | None = None, 
                  error: str | None = None) -> None:
    """Update job state. Called by PipelineRunner after each stage."""

def mark_completed(job_id: str, video_id: str) -> None:
    """Finalize job, create upload record."""

def get_job_history(limit: int = 50, status: str | None = None) -> list[Job]:
    """Retrieve recent jobs, optionally filtered by status."""

def retry_failed_job(job_id: str) -> None:
    """Reset status to pending, clear error_message."""
```

**Status transitions:**
```
pending → running → completed
                 → failed (retryable via retry_failed_job)
```

**Why this design:** Decouples job persistence from execution logic; allows dashboard and worker to operate independently.

### 4.3 PipelineRunner (`app/core/pipeline_runner.py`)

**Responsibilities:**
- Execute existing pipeline logic for a job
- Automatically assign account if not set
- Update job status/stage in DB as pipeline progresses
- Handle errors and quota exhaustion gracefully
- Consume quota on successful upload

**Key method:**
```python
def run_job(job: Job) -> bool:
    """
    Execute full pipeline for the given job.
    
    Steps:
    1. Assign account if job.account_id is None
    2. Update job status=running
    3. Call orchestrator.run_pipeline(job.prompt, job.job_id, account_dict)
    4. After each stage: update job.stage in DB
    5. On success:
       - consume_quota(account_id, 1600)
       - create upload record
       - mark_completed(job_id, video_id)
    6. On error:
       - update status=failed + error_message
       - if YouTubeQuotaExceeded: don't consume quota, mark for retry
    
    Returns:
        True if successful, False if failed
    """
```

**Integration with existing orchestrator:**

Refactor `app/orchestrator.py` to export callable function:

```python
# app/orchestrator.py (modified)
def run_pipeline(prompt: str, job_id: str | None = None, 
                 account: dict | None = None) -> dict:
    """
    Execute pipeline stages: script → tts → captions → visual → render → upload.
    
    Args:
        prompt: User input text
        job_id: Override auto-generated job_id (optional)
        account: Dict with keys: client_id, client_secret, refresh_token (optional)
    
    Returns:
        {
            "success": bool,
            "job_id": str,
            "video_id": str | None,
            "stages_completed": list[str],
            "error": str | None
        }
    """
    # Extract existing logic, return structured result
    ...

def main():
    """CLI entry point (preserves backward compatibility)"""
    args = parse_args()
    result = run_pipeline(args.prompt, args.job_id)
    if not result["success"]:
        sys.exit(1)
```

**Why this design:** Bridges DB-aware core logic with existing file-based pipeline without breaking CLI; enables granular status updates for dashboard polling.

### 4.4 AnalyticsAggregator (`app/core/analytics.py`)

**Responsibilities:**
- Summarize data from `uploads` and `quota_snapshots` tables
- Provide pre-computed metrics for dashboard display
- Optionally sync YouTube video views via API

**Key methods:**
```python
def get_account_stats(account_id: int) -> dict:
    """
    Returns:
        {
            "total_uploads": int,
            "total_views": int,
            "avg_quota_usage": float,
            "last_upload": datetime | None
        }
    """

def get_daily_summary(date: date) -> dict:
    """
    Returns:
        {
            "jobs_completed": int,
            "total_quota_used": int,
            "uploads_by_account": dict[str, int]
        }
    """

def get_recent_uploads(limit: int = 10) -> list[Upload]:
    """Return most recent uploads across all accounts."""

def sync_youtube_views() -> None:
    """
    (Optional - Phase 2)
    Call YouTube Data API videos.list for each uploaded video,
    update uploads.views column.
    """
```

**Why this design:** Keeps expensive aggregation queries out of route handlers; enables caching strategies later if needed.

---

## 5. Web Dashboard (FastAPI)

### 5.1 Technology Stack

**Backend:**
- FastAPI 0.115+ (async, auto OpenAPI docs, fast)
- Uvicorn (ASGI server)
- Jinja2 (server-side templates)
- SQLAlchemy 2.0 (ORM)

**Frontend:**
- Jinja2 templates (HTML generation)
- TailwindCSS via CDN (rapid prototyping)
- Vanilla JavaScript + htmx (dynamic updates without page reloads)
- Chart.js (simple graphs)

**Why not React/Vue?** For 2-5 accounts and 10-30 videos/day, server-side rendering with htmx provides 80% of the UX with 20% of the code complexity. The FastAPI backend is already API-ready if a separate frontend becomes necessary later.

### 5.2 Routes

#### Dashboard Routes (`app/web/routes/dashboard.py`)
- `GET /` → Home page with overview cards and recent activity

#### Account Routes (`app/web/routes/accounts.py`)
- `GET /accounts` → List all accounts (table with status badges)
- `GET /accounts/new` → Form to add new account
- `POST /accounts` → Create account (validates token first)
- `GET /accounts/{id}/edit` → Edit form
- `POST /accounts/{id}/edit` → Update account
- `POST /accounts/{id}/toggle` → Enable/disable account
- `GET /accounts/{id}/stats` → Detailed analytics for one account

#### Job Routes (`app/web/routes/jobs.py`)
- `GET /jobs` → List jobs (filters: status, account_id)
- `GET /jobs/new` → Form to submit new prompt
- `POST /jobs` → Enqueue job
- `GET /jobs/{job_id}` → Job details (logs, artifacts, YouTube link)
- `POST /jobs/{job_id}/retry` → Retry failed job
- `DELETE /jobs/{job_id}` → Cancel pending job

#### API Routes (`app/web/routes/api.py`)
JSON endpoints for AJAX/polling:
- `GET /api/status` → System health snapshot
- `GET /api/jobs/{job_id}/status` → Current job state (for polling)
- `GET /api/accounts/{id}/quota` → Real-time quota for one account

### 5.3 Page Designs

#### `dashboard.html` (Home `/`)
**Content:**
- **Cards row:**
  - Active accounts (X / Y)
  - Jobs in queue (pending count)
  - Available quota (sum across active accounts)
  - Today's uploads
- **Chart:** Uploads per day (last 7 days) - Chart.js bar chart
- **Recent jobs table:** Last 5 jobs (mini version with status badges)

**Purpose:** Quick health check at a glance.

#### `accounts.html` (`/accounts`)
**Content:**
- **Table columns:** Name, Email, Quota Used/Total, Status, Actions
- **Status badges:**
  - 🟢 Green: quota > 3200 available
  - 🟡 Yellow: quota 1600-3200 (low)
  - 🔴 Red: quota < 1600 (exhausted)
  - ⚫ Gray: inactive
- **Actions:** Edit, Toggle (activate/deactivate)
- **Button:** "Add Account" (opens form modal or new page)

**Purpose:** Manage account pool and monitor quota status.

#### `account_form.html` (Add/Edit)
**Form fields:**
- Name (text, unique)
- Email (text)
- Client ID (text)
- Client Secret (password)
- Refresh Token (textarea)
- Daily Quota (number, default 10000)

**Validation:**
- "Test Credentials" button: AJAX call to `/api/accounts/validate` → calls `youtube_service.verify_credentials`
- Shows ✅ or ❌ before allowing save

**Purpose:** Ensure credentials are valid before persisting.

#### `jobs.html` (`/jobs`)
**Content:**
- **Filters:** Status dropdown (all/pending/running/completed/failed), Account dropdown
- **Table columns:** Job ID (truncated), Prompt (first 50 chars), Account, Status, Stage, Created At
- **Status badges:**
  - 🔵 Pending
  - 🟡 Running (pulsating animation)
  - 🟢 Completed
  - 🔴 Failed
- **Actions:** View Details, Retry (if failed), Cancel (if pending)
- **Button:** "New Job" (opens form)

**Purpose:** Monitor pipeline execution and manage queue.

#### `job_detail.html` (`/jobs/{job_id}`)
**Content:**
- **Metadata:** Job ID, Prompt (full), Account used, Priority
- **Timeline:** Stage progress (script ✅ → tts ✅ → captions ✅ → visual ✅ → render ✅ → upload ✅)
- **Artifacts:** Links to download `script.json`, `final.mp4`, etc.
- **If completed:** YouTube video link (opens in new tab)
- **If failed:** Error message (code block), "Retry" button
- **Auto-refresh:** Polls `/api/jobs/{job_id}/status` every 3 seconds if status=running

**Purpose:** Deep dive into job execution; enables troubleshooting.

### 5.4 Authentication

**Phase 1 (MVP):** No authentication. Dashboard runs on `localhost:8000` only (assumes trusted environment).

**Phase 2 (if exposed on network):** Add HTTP Basic Auth via FastAPI middleware (username/password in `.env`), or OAuth via Google (reuse existing OAuth setup).

**Why defer auth?** Speeds up initial implementation; easy to add later via FastAPI dependencies.

---

## 6. Worker Process

### 6.1 Simple Polling Loop

**`app/worker.py`:**
```python
"""
Job worker: polls database for pending jobs and executes them.
Run separately from dashboard:
    python -m app.worker
"""
import time
from app.db.database import get_session
from app.core.job_queue import JobQueueManager
from app.core.pipeline_runner import PipelineRunner

def main():
    print("[worker] Starting job processor...")
    while True:
        with get_session() as session:
            queue = JobQueueManager(session)
            runner = PipelineRunner(session)
            
            job = queue.get_next_pending()
            if job:
                print(f"[worker] Processing job {job.job_id}...")
                success = runner.run_job(job)
                if success:
                    print(f"[worker] Job {job.job_id} completed successfully")
                else:
                    print(f"[worker] Job {job.job_id} failed: {job.error_message}")
            else:
                # No jobs available, sleep and retry
                time.sleep(5)

if __name__ == "__main__":
    main()
```

**Running the system:**
```bash
# Terminal 1: Dashboard
uvicorn app.web.main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2: Worker
python -m app.worker
```

**Why polling instead of Celery?** For 10-30 videos/day, a simple loop is sufficient and avoids Redis dependency. Can upgrade to Celery later if parallel processing becomes necessary.

### 6.2 Quota Reset

**Problem:** YouTube API quota resets at 00:00 Pacific Time (~04:00 UTC).

**Solution:** Worker checks quota reset times at start of each loop:
```python
# In worker main loop, before getting next job:
AccountManager(session).reset_daily_quotas()
```

**Alternative:** Cron job calls CLI script:
```bash
# crontab entry (runs at 04:05 UTC daily)
5 4 * * * cd /path/to/repo && python -c "from app.core.account_manager import reset_daily_quotas; reset_daily_quotas()"
```

---

## 7. Data Flow: Job Submission to Upload

### 7.1 User submits job via dashboard

```
1. User fills form at /jobs/new → prompt + priority
   ↓
2. POST /jobs → JobQueueManager.enqueue(prompt, priority)
   ↓
3. DB insert: jobs(status=pending, account_id=NULL)
   ↓
4. Redirect to /jobs (user sees job in queue)
   ↓
5. Worker loop detects pending job
   ↓
6. PipelineRunner.run_job(job):
   a) AccountManager.get_available_account() → assign to job
   b) Update job(status=running, account_id=X)
   c) Call orchestrator.run_pipeline(prompt, job_id, account)
   d) After each stage: update job.stage in DB
   e) On success:
      - AccountManager.consume_quota(account_id, 1600)
      - Insert uploads record
      - JobQueueManager.mark_completed(job_id, video_id)
   f) On error:
      - Update job(status=failed, error_message=...)
   ↓
7. Dashboard polls /api/jobs/{job_id}/status → updates UI in real-time
   ↓
8. User clicks job in table → /jobs/{job_id} → sees details + YouTube link
```

### 7.2 CLI workflow (unchanged)

```
1. User runs: python -m app.orchestrator --prompt "..."
   ↓
2. orchestrator.main() calls run_pipeline() directly
   ↓
3. Pipeline executes (same as always)
   ↓
4. Files written to assets/generated/ and assets/rendered/
   ↓
5. Upload to YouTube (uses default account from .env or first active in DB)
```

**Key insight:** CLI and dashboard can coexist. CLI doesn't touch DB; dashboard/worker use DB + call same underlying pipeline code.

---

## 8. Error Handling and Resilience

### 8.1 Service-level fallbacks (already implemented)
- Pollinations 429 → Imagen (fixed in recent commit)
- Groq rate limit → retry with exponential backoff

### 8.2 Job-level error handling (new)

**Scenario: YouTube quota exhausted mid-upload**
```python
# In PipelineRunner.run_job
try:
    result = orchestrator.run_pipeline(...)
except YouTubeQuotaExceeded:
    # Don't mark as failed; job is still valid
    job.status = "pending"
    job.account_id = None  # unassign, let next run pick different account
    job.error_message = "Quota exhausted on upload, will retry with another account"
    # Worker will re-pick this job with a different account
except Exception as e:
    job.status = "failed"
    job.error_message = str(e)
    # User can manually retry via dashboard
```

**Scenario: OAuth refresh token invalid**
```python
# In AccountManager.add_account or verify flow
try:
    youtube_service.verify_credentials(refresh_token)
except InvalidTokenError:
    account.is_active = False
    account.error_message = "Refresh token expired or revoked. Re-run bootstrap_youtube_oauth.py"
    # Dashboard shows red alert on /accounts page
```

### 8.3 Retry strategies

**Automatic retry:** YouTube quota exhausted → job stays pending, tries different account  
**Manual retry:** Failed jobs show "Retry" button in dashboard → resets status to pending  
**No retry:** Invalid credentials, FFmpeg crash → requires user intervention

---

## 9. Migration Plan (Zero Downtime)

### 9.1 Phases

**Phase 1: Database setup (Day 1-2)**
- Create `app/db/`, implement models
- Write `scripts/init_db.py` to create tables
- Migrate current `.env` credentials to `accounts` table (manual or migration script)

**Phase 2: Core logic (Day 3-4)**
- Implement AccountManager, JobQueueManager, PipelineRunner, AnalyticsAggregator
- Refactor `orchestrator.py` to export `run_pipeline()` function
- Write unit tests for core components
- CLI still works (uses first active account from DB or falls back to `.env`)

**Phase 3: Web dashboard (Day 5-6)**
- Implement FastAPI routes + Jinja2 templates
- Test job submission via web
- Implement worker loop
- Integration testing (submit job via dashboard, verify it processes)

**Phase 4: Polish (Day 7)**
- Add analytics charts (Chart.js)
- Improve UI/UX (status badges, auto-refresh)
- Write documentation (README updates, setup guide)
- End-to-end smoke test with real YouTube upload

**During migration:**
- Existing CLI **never stops working**
- Can run CLI and dashboard/worker in parallel
- No data loss (files remain in `assets/`, DB is additive)

### 9.2 Rollback strategy

If dashboard/worker has critical bug:
1. Stop worker process
2. Continue using CLI as before
3. Fix bug, redeploy
4. Database remains intact (jobs can be retried)

---

## 10. Testing Strategy

### 10.1 Unit tests (new)

**`tests/test_account_manager.py`**
- `test_add_account_validates_token`
- `test_get_available_account_prefers_highest_quota`
- `test_consume_quota_marks_unavailable_when_exhausted`
- `test_reset_daily_quotas_updates_timestamp`

**`tests/test_job_queue.py`**
- `test_enqueue_creates_pending_job`
- `test_get_next_pending_respects_priority`
- `test_update_status_transitions_correctly`
- `test_retry_failed_job_resets_state`

**`tests/test_pipeline_runner.py`**
- `test_run_job_assigns_account_automatically`
- `test_run_job_updates_stages_in_db`
- `test_run_job_consumes_quota_on_success`
- `test_run_job_handles_quota_exhausted` (mock YouTube API)

### 10.2 Integration tests

**`tests/test_web_routes.py`**
- `test_submit_job_via_dashboard` (uses FastAPI TestClient)
- `test_job_detail_page_shows_artifacts`
- `test_account_form_validates_token` (mock verify_credentials)

### 10.3 End-to-end smoke test

Manual checklist (documented in README):
1. `python scripts/init_db.py`
2. Add test account via dashboard (`/accounts/new`)
3. Submit job via dashboard (`/jobs/new`)
4. Watch worker process in terminal
5. Verify video uploaded to YouTube (private)
6. Check analytics on dashboard (`/accounts/{id}/stats`)

---

## 11. New Dependencies

**Add to `requirements.txt`:**
```txt
# Web Dashboard
fastapi==0.115.0
uvicorn[standard]==0.32.0
jinja2==3.1.4
python-multipart==0.0.17

# Database
sqlalchemy==2.0.36
alembic==1.14.0              # (optional - for migrations in Phase 2)

# Testing
httpx==0.27.2                # FastAPI test client
pytest-asyncio==0.24.0       # async test support
```

**Update `.env.example`:**
```bash
# Existing variables (keep all)
...

# Database
DATABASE_URL=sqlite:///data/app.db
```

---

## 12. Setup for New Users

### 12.1 Initial setup (after cloning repo)

```bash
# Existing steps (unchanged)
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash
pip install -r requirements.txt

# NEW: Initialize database
python scripts/init_db.py

# NEW: Add first account (interactive prompt)
python -m app.core.account_manager add-account
# OR add via dashboard after starting it
```

### 12.2 Running the system

**CLI mode (existing workflow):**
```bash
python -m app.orchestrator --prompt "..."
```

**Dashboard mode (new workflow):**
```bash
# Terminal 1: Start dashboard
uvicorn app.web.main:app --reload

# Terminal 2: Start worker
python -m app.worker

# Open browser: http://localhost:8000
```

---

## 13. Monitoring and Health Checks

### 13.1 System health snapshot

**`GET /api/status` returns:**
```json
{
  "accounts_active": 3,
  "accounts_total": 5,
  "jobs_pending": 2,
  "jobs_running": 1,
  "quota_available": 7400,
  "last_upload": "2026-05-01T10:30:00Z",
  "warnings": [
    "Account 'Conta 2' quota low (800 remaining)",
    "Account 'Conta 5' token expired - needs re-auth"
  ]
}
```

**Dashboard home page uses this to show:**
- 🔴 Critical: No accounts active
- 🟡 Warning: Quota total < 3200 (< 2 uploads possible)
- 🟢 Healthy: Multiple accounts with quota available

### 13.2 Alerting (future enhancement)

Could add:
- Email notification when all accounts exhausted
- Slack webhook on job failures
- Log aggregation (ELK stack or simple file rotation)

---

## 14. Future Enhancements (Out of Scope)

### 14.1 If scaling beyond 30 videos/day

**Replace SQLite with PostgreSQL:**
- Change `DATABASE_URL` in `.env`
- SQLAlchemy abstracts the difference (no code changes)

**Add Celery for parallel processing:**
- Replace worker polling loop with Celery tasks
- Add Redis as message broker
- Enables multiple workers processing jobs concurrently

**React/Vue frontend:**
- FastAPI already serves pure API (`/api/*` routes)
- Build SPA that consumes these endpoints
- Better UX: real-time WebSocket updates, richer visualizations

### 14.2 Additional features

**Prompt templates:**
- Table `prompt_templates` with variables like `{topic}`
- Dashboard allows "Top 10 Facts about {topic}" → fill variable → enqueue

**YouTube Analytics sync:**
- Cron job calls YouTube API `videos.list` to fetch view counts
- Updates `uploads.views` column
- Dashboard shows trending videos

**Multi-language support:**
- Add `language` field to jobs
- Pass to script_service and caption_service

**Bulk operations:**
- Upload CSV of prompts → enqueue 50 jobs at once
- Useful for content batching

---

## 15. Security Considerations

### 15.1 Credential storage

**Current:** Refresh tokens stored in plaintext in SQLite.

**Risk:** If `data/app.db` is compromised, attacker gains YouTube upload access.

**Mitigations (Phase 2):**
- Encrypt sensitive fields (refresh_token, client_secret) with Fernet (cryptography library)
- Store encryption key in `.env` (never commit)
- For production: use secrets manager (AWS Secrets Manager, HashiCorp Vault)

### 15.2 Dashboard access

**Phase 1:** No auth (localhost only)

**Phase 2:** Add one of:
- HTTP Basic Auth (simple, good enough for trusted network)
- OAuth via Google (reuse existing OAuth flow)
- API keys (if building integrations)

### 15.3 Input validation

**SQL injection:** SQLAlchemy ORM parameterizes queries automatically (safe by default)

**XSS in dashboard:** Jinja2 auto-escapes variables (safe by default)

**Prompt injection:** Script service already has content moderation (existing `visual_service._moderate`)

---

## 16. Success Metrics

### 16.1 MVP acceptance criteria

- [ ] Dashboard runs on `localhost:8000`
- [ ] Can add/edit/toggle accounts via web UI
- [ ] Can submit jobs via web form
- [ ] Worker processes jobs automatically
- [ ] Job detail page shows stage progress in real-time
- [ ] Analytics page shows uploads per day chart
- [ ] CLI workflow still works (backward compatible)
- [ ] Unit tests pass for all core components
- [ ] End-to-end smoke test uploads video to YouTube

### 16.2 Performance targets

- Job submission (web) → enqueued: < 200ms
- Worker picks up pending job: < 10 seconds after enqueue
- Dashboard page load: < 500ms (local network)
- Database queries (analytics): < 100ms (SQLite sufficient for 100s of jobs)

### 16.3 Reliability targets

- Worker uptime: 99%+ (simple process, restart on crash via systemd/supervisor)
- Job success rate: 90%+ (excluding quota exhaustion, which retries automatically)
- Zero data loss: All jobs and uploads recorded (DB + file artifacts)

---

## 17. Documentation Updates

### 17.1 README additions

**New sections to add:**
- "Running with Dashboard" (setup + usage guide)
- "Managing Multiple Accounts" (how to add, quota tracking)
- "Job Queue System" (priority, retry, status meanings)
- "API Reference" (link to FastAPI auto-docs at `/docs`)

### 17.2 Architecture diagram

Add Mermaid diagram showing:
```
User → Dashboard → DB ← Worker → Pipeline → YouTube
       ↓
      CLI → Pipeline → YouTube (direct path)
```

### 17.3 Troubleshooting additions

- "Worker not processing jobs" → check DB connection, account availability
- "Dashboard shows 'No accounts active'" → add account or check tokens
- "Job stuck in 'running' status" → worker crashed, restart worker

---

## 18. Conclusion

This design delivers a scalable multi-account system while preserving the working MVP. Key strengths:

1. **Incremental migration:** Dashboard and CLI coexist; no big-bang rewrite
2. **Pragmatic choices:** SQLite + polling loop sufficient for 10-30 videos/day
3. **Clear upgrade path:** SQLite → Postgres, polling → Celery when needed
4. **Testable:** Core logic isolated from web/DB layers
5. **User-friendly:** Web UI lowers barrier to managing accounts and monitoring jobs

**Implementation time:** ~7 days for full-stack developer familiar with Python

**Next step:** Approve this spec → invoke `writing-plans` skill to generate detailed implementation plan with file-by-file checklist.
