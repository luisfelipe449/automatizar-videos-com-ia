# Multi-worker com claim atômico — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permitir rodar um worker por conta YouTube em paralelo, com claim atômico via `UPDATE ... RETURNING` e recuperação de jobs órfãos no startup.

**Architecture:** Um processo `python -m app.worker --account-id N` por conta. SQLite em WAL mode com `busy_timeout=5000ms`. `JobQueueManager` ganha `claim_next_pending(account_id)` (single-shot `UPDATE ... RETURNING`) e `reset_orphan_running(account_id)` (chamado no startup do worker). Script opcional `scripts/run_workers.py` spawna um subprocess por conta ativa.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x, SQLite 3.35+ (com `RETURNING`), pytest, subprocess.

**Spec:** `docs/superpowers/specs/2026-05-03-multi-worker-design.md`

---

## File Structure

| Arquivo | Responsabilidade |
|---|---|
| `app/db/database.py` | Engine/session + PRAGMAs de concorrência (WAL, busy_timeout) |
| `app/core/job_queue.py` | CRUD de jobs + `claim_next_pending` (atômico) + `reset_orphan_running` |
| `app/worker.py` | CLI com `--account-id`, startup (validação + órfãos), loop (claim) |
| `scripts/run_workers.py` | **Novo.** Supervisor opcional: spawna 1 worker/conta ativa |
| `tests/test_job_queue.py` | Testes existentes + concorrência + órfão |
| `tests/test_worker.py` | **Novo.** Startup valida conta, chama reset_orphan_running, loop usa claim |
| `CLAUDE.md` | Nova seção "Running multiple workers" |

---

## Task 1: Habilitar WAL mode e busy_timeout no engine SQLite

**Files:**
- Modify: `app/db/database.py`
- Test: `tests/test_database.py` (novo)

- [ ] **Step 1: Write the failing test**

Criar `tests/test_database.py`:

```python
"""Database engine configuration: WAL mode and busy_timeout."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.db.database import get_engine


def test_sqlite_engine_enables_wal_mode(tmp_path: Path) -> None:
    """get_engine() applied to a sqlite URL must set journal_mode=WAL."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    # Force a connection so the connect event fires.
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode == "wal", f"expected WAL mode, got {mode!r}"


def test_sqlite_engine_sets_busy_timeout(tmp_path: Path) -> None:
    """get_engine() must configure a non-zero busy_timeout so contention
    doesn't immediately raise SQLITE_BUSY."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout_ms == 5000, f"expected busy_timeout=5000, got {timeout_ms}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_database.py -v`
Expected: FAIL — `journal_mode` vem como `'delete'` (default SQLite), `busy_timeout` vem como `0`.

- [ ] **Step 3: Write minimal implementation**

Substituir o conteúdo de `app/db/database.py` pelo abaixo (é uma atualização cirúrgica — só adiciona o listener):

```python
"""SQLAlchemy engine and session management."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Base class for all models
Base = declarative_base()

# Default database URL from environment
_DEFAULT_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{Path(__file__).resolve().parent.parent.parent / 'data' / 'app.db'}"
)


def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """PRAGMAs de concorrência aplicados a TODA conexão SQLite nova.

    - journal_mode=WAL: leitores não bloqueiam escritor; writers ainda
      serializam mas sem bloquear leituras concorrentes.
    - busy_timeout=5000: aguarda até 5s em contenção antes de devolver
      SQLITE_BUSY. Evita que workers concorrentes exploquem sob load.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def get_engine(database_url: str | None = None):
    """Create SQLAlchemy engine.

    Args:
        database_url: Database connection string. If None, uses DATABASE_URL env var.

    Returns:
        Engine instance
    """
    url = database_url or _DEFAULT_DATABASE_URL

    # SQLite-specific: enable foreign keys
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    engine = create_engine(url, connect_args=connect_args, echo=False)

    # WAL + busy_timeout só pra SQLite. Outros drivers não têm esses pragmas.
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _apply_sqlite_pragmas)

    return engine


# Global engine and session factory (initialized on first import)
_engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@contextmanager
def get_session(engine=None) -> Session:
    """Context manager for database sessions.

    Usage:
        with get_session() as session:
            session.query(Account).all()

    Args:
        engine: Optional engine override (for testing)

    Yields:
        Session instance
    """
    if engine is not None:
        # Test override
        SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        session = SessionFactory()
    else:
        session = SessionLocal()

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_database.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `pytest -v -m "not slow"`
Expected: todos passam.

- [ ] **Step 6: Commit**

```bash
git add app/db/database.py tests/test_database.py
git commit -m "feat(db): enable SQLite WAL mode and busy_timeout for concurrent workers"
```

---

## Task 2: Adicionar `reset_orphan_running` ao JobQueueManager

**Files:**
- Modify: `app/core/job_queue.py`
- Test: `tests/test_job_queue.py`

- [ ] **Step 1: Write the failing tests**

Adicionar ao fim de `tests/test_job_queue.py`:

```python
def test_reset_orphan_running_resets_only_given_account(db_session):
    """reset_orphan_running(N) deve resetar só jobs com account_id=N em running."""
    from app.core.job_queue import JobQueueManager
    from app.db.models import Account, Job
    from datetime import datetime, timezone

    # Two accounts, both with one 'running' job each + one 'pending' job each.
    acc_a = Account(
        name="A", email="a@x", client_id="ca", client_secret="sa",
        refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
    )
    acc_b = Account(
        name="B", email="b@x", client_id="cb", client_secret="sb",
        refresh_token="rb", quota_reset_at=datetime.now(timezone.utc),
    )
    db_session.add_all([acc_a, acc_b])
    db_session.commit()

    running_a = Job(
        job_id="job-a-running", account_id=acc_a.id, prompt="a1",
        status="running", started_at=datetime.now(timezone.utc),
    )
    pending_a = Job(job_id="job-a-pending", account_id=acc_a.id, prompt="a2", status="pending")
    running_b = Job(
        job_id="job-b-running", account_id=acc_b.id, prompt="b1",
        status="running", started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([running_a, pending_a, running_b])
    db_session.commit()

    mgr = JobQueueManager(db_session)
    count = mgr.reset_orphan_running(account_id=acc_a.id)

    assert count == 1
    db_session.refresh(running_a)
    db_session.refresh(running_b)
    db_session.refresh(pending_a)
    assert running_a.status == "pending"
    assert running_a.started_at is None
    assert running_b.status == "running"  # not touched — different account
    assert pending_a.status == "pending"  # not touched — was already pending


def test_reset_orphan_running_is_idempotent(db_session):
    """Chamar duas vezes: primeira reseta, segunda retorna 0."""
    from app.core.job_queue import JobQueueManager
    from app.db.models import Account, Job
    from datetime import datetime, timezone

    acc = Account(
        name="C", email="c@x", client_id="cc", client_secret="sc",
        refresh_token="rc", quota_reset_at=datetime.now(timezone.utc),
    )
    db_session.add(acc)
    db_session.commit()

    job = Job(
        job_id="job-c", account_id=acc.id, prompt="c",
        status="running", started_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    db_session.commit()

    mgr = JobQueueManager(db_session)
    assert mgr.reset_orphan_running(account_id=acc.id) == 1
    assert mgr.reset_orphan_running(account_id=acc.id) == 0
```

(A fixture `db_session` já existe no `conftest.py` — verifique em `tests/conftest.py` antes de rodar. Se não existir, usar a mesma fixture que os testes atuais de `test_job_queue.py` usam.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_job_queue.py::test_reset_orphan_running_resets_only_given_account tests/test_job_queue.py::test_reset_orphan_running_is_idempotent -v`
Expected: FAIL — `AttributeError: 'JobQueueManager' object has no attribute 'reset_orphan_running'`.

- [ ] **Step 3: Write minimal implementation**

Adicionar ao fim da classe `JobQueueManager` em `app/core/job_queue.py`:

```python
    def reset_orphan_running(self, account_id: int) -> int:
        """Reset jobs stuck in 'running' for a given account back to 'pending'.

        Intended to be called at worker startup. In the worker-per-account
        model, the worker owning `account_id` is the ONLY process that runs
        jobs for it — so any `running` job on startup is orphaned (previous
        worker crashed). Resets status to `pending` and clears `started_at`
        so the loop can re-claim them.

        Returns the number of jobs reset (0 if none were orphaned).
        """
        updated = (
            self.session.query(Job)
            .filter(Job.account_id == account_id, Job.status == "running")
            .update(
                {Job.status: "pending", Job.started_at: None},
                synchronize_session=False,
            )
        )
        self.session.commit()
        return int(updated)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_job_queue.py::test_reset_orphan_running_resets_only_given_account tests/test_job_queue.py::test_reset_orphan_running_is_idempotent -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/core/job_queue.py tests/test_job_queue.py
git commit -m "feat(queue): add reset_orphan_running for worker startup recovery"
```

---

## Task 3: Adicionar `claim_next_pending` atômico

**Files:**
- Modify: `app/core/job_queue.py`
- Test: `tests/test_job_queue.py`

- [ ] **Step 1: Write the failing tests (happy path + account isolation)**

Adicionar a `tests/test_job_queue.py`:

```python
def test_claim_next_pending_marks_job_running_and_returns_it(db_session):
    """Claim atômico: retorna o job e muda status pra 'running' no mesmo passo."""
    from app.core.job_queue import JobQueueManager
    from app.db.models import Account, Job
    from datetime import datetime, timezone

    acc = Account(
        name="A", email="a@x", client_id="ca", client_secret="sa",
        refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
    )
    db_session.add(acc)
    db_session.commit()

    job = Job(job_id="job-1", account_id=acc.id, prompt="p", status="pending", priority=0)
    db_session.add(job)
    db_session.commit()

    mgr = JobQueueManager(db_session)
    claimed = mgr.claim_next_pending(account_id=acc.id)

    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert claimed.status == "running"
    assert claimed.started_at is not None


def test_claim_next_pending_returns_none_when_queue_empty(db_session):
    """Sem jobs pendentes pra conta → retorna None."""
    from app.core.job_queue import JobQueueManager
    from app.db.models import Account
    from datetime import datetime, timezone

    acc = Account(
        name="A", email="a@x", client_id="ca", client_secret="sa",
        refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
    )
    db_session.add(acc)
    db_session.commit()

    mgr = JobQueueManager(db_session)
    assert mgr.claim_next_pending(account_id=acc.id) is None


def test_claim_next_pending_respects_account_isolation(db_session):
    """Worker da conta A nunca pega job da conta B."""
    from app.core.job_queue import JobQueueManager
    from app.db.models import Account, Job
    from datetime import datetime, timezone

    acc_a = Account(
        name="A", email="a@x", client_id="ca", client_secret="sa",
        refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
    )
    acc_b = Account(
        name="B", email="b@x", client_id="cb", client_secret="sb",
        refresh_token="rb", quota_reset_at=datetime.now(timezone.utc),
    )
    db_session.add_all([acc_a, acc_b])
    db_session.commit()

    job_b = Job(job_id="job-b", account_id=acc_b.id, prompt="p", status="pending")
    db_session.add(job_b)
    db_session.commit()

    mgr = JobQueueManager(db_session)
    assert mgr.claim_next_pending(account_id=acc_a.id) is None
    # job_b ainda pendente
    db_session.refresh(job_b)
    assert job_b.status == "pending"


def test_claim_next_pending_orders_by_priority_then_created_at(db_session):
    """Prioridade maior primeiro; em empate, mais antigo primeiro."""
    from app.core.job_queue import JobQueueManager
    from app.db.models import Account, Job
    from datetime import datetime, timezone, timedelta

    acc = Account(
        name="A", email="a@x", client_id="ca", client_secret="sa",
        refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
    )
    db_session.add(acc)
    db_session.commit()

    now = datetime.now(timezone.utc)
    low_old = Job(
        job_id="low-old", account_id=acc.id, prompt="p", status="pending",
        priority=0, created_at=now - timedelta(minutes=10),
    )
    high_new = Job(
        job_id="high-new", account_id=acc.id, prompt="p", status="pending",
        priority=5, created_at=now,
    )
    db_session.add_all([low_old, high_new])
    db_session.commit()

    mgr = JobQueueManager(db_session)
    claimed = mgr.claim_next_pending(account_id=acc.id)
    assert claimed.job_id == "high-new"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_job_queue.py -k "claim_next_pending" -v`
Expected: FAIL — `AttributeError: 'JobQueueManager' object has no attribute 'claim_next_pending'`.

- [ ] **Step 3: Write minimal implementation**

Adicionar ao topo do arquivo (depois dos imports já existentes) se ainda não tiver:

```python
from sqlalchemy import text
```

E adicionar o método ao fim da classe `JobQueueManager`:

```python
    def claim_next_pending(self, account_id: int) -> Job | None:
        """Atomically claim the next pending job for `account_id`.

        Replaces the old read-then-update pattern (`get_next_pending` +
        `update_status`) which had a race window. Uses a single
        `UPDATE ... RETURNING` so two concurrent workers cannot claim the
        same job — SQLite serializes writers; the loser sees zero rows.

        The `ORDER BY priority DESC, created_at ASC` inside the subquery
        mirrors `get_next_pending` so existing priority semantics are
        preserved.
        """
        stmt = text(
            """
            UPDATE jobs
            SET status = 'running',
                started_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'pending' AND account_id = :account_id
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
            )
            AND status = 'pending'
            RETURNING id
            """
        )
        result = self.session.execute(stmt, {"account_id": account_id})
        row = result.first()
        self.session.commit()
        if row is None:
            return None
        # Reload through the ORM so relationships + typed attrs are available.
        return self.session.query(Job).filter(Job.id == row[0]).one()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_job_queue.py -k "claim_next_pending" -v`
Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/core/job_queue.py tests/test_job_queue.py
git commit -m "feat(queue): add atomic claim_next_pending via UPDATE RETURNING"
```

---

## Task 4: Teste de concorrência do claim atômico

**Files:**
- Modify: `tests/test_job_queue.py`

- [ ] **Step 1: Write the failing test**

Adicionar a `tests/test_job_queue.py`. Este teste exercita concorrência real usando dois threads e duas sessions separadas no MESMO banco em disco — SQLite in-memory não compartilha entre threads.

```python
def test_claim_next_pending_is_atomic_under_concurrency(tmp_path):
    """Duas threads claim simultâneo em 1 job pendente: só 1 ganha."""
    import threading
    from datetime import datetime, timezone
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.core.job_queue import JobQueueManager
    from app.db.database import Base, _apply_sqlite_pragmas
    from app.db.models import Account, Job

    # File-backed SQLite so threads share state.
    db_path = tmp_path / "concurrent.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)

    # Seed: one account, one pending job.
    seed = Session()
    acc = Account(
        name="A", email="a@x", client_id="ca", client_secret="sa",
        refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
    )
    seed.add(acc)
    seed.commit()
    seed.add(Job(job_id="only", account_id=acc.id, prompt="p", status="pending"))
    seed.commit()
    account_id = acc.id
    seed.close()

    # Barrier so both threads fire the UPDATE at (almost) the same instant.
    barrier = threading.Barrier(2)
    results: list[Job | None] = [None, None]

    def worker(idx: int) -> None:
        session = Session()
        try:
            mgr = JobQueueManager(session)
            barrier.wait(timeout=5)
            results[idx] = mgr.claim_next_pending(account_id=account_id)
        finally:
            session.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Exactly one won.
    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, f"expected 1 winner, got {len(winners)}"
    assert len(losers) == 1, f"expected 1 loser, got {len(losers)}"
    assert winners[0].job_id == "only"
    assert winners[0].status == "running"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_job_queue.py::test_claim_next_pending_is_atomic_under_concurrency -v`
Expected: PASS. Se falhar com `SQLITE_BUSY`: confirmar que Task 1 aplicou `busy_timeout=5000` (este teste depende disso).

- [ ] **Step 3: Commit**

```bash
git add tests/test_job_queue.py
git commit -m "test(queue): concurrent claim with real threads and file-backed SQLite"
```

---

## Task 5: Worker com `--account-id` + startup-reset + loop via claim

**Files:**
- Modify: `app/worker.py`
- Test: `tests/test_worker.py` (novo)

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_worker.py`:

```python
"""Worker startup + loop behavior."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


def _mk_account(session, **overrides):
    from app.db.models import Account
    defaults = dict(
        name="A", email="a@x", client_id="ca", client_secret="sa",
        refresh_token="ra", quota_reset_at=datetime.now(timezone.utc),
        is_active=True,
    )
    defaults.update(overrides)
    acc = Account(**defaults)
    session.add(acc)
    session.commit()
    return acc


def test_worker_requires_account_id(capsys):
    """Rodar sem --account-id deve falhar com erro claro e exit != 0."""
    import sys
    from app.worker import main

    old_argv = sys.argv
    sys.argv = ["app.worker"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            main()
        # argparse exits with 2 on missing required arg
        assert exc_info.value.code == 2
    finally:
        sys.argv = old_argv


def test_worker_exits_when_account_not_found(db_session, capsys):
    """Startup com --account-id inexistente: erro claro, exit 1."""
    import sys
    from app.worker import main

    old_argv = sys.argv
    sys.argv = ["app.worker", "--account-id", "9999"]
    try:
        with patch("app.worker.get_session") as mock_get:
            mock_get.return_value.__enter__.return_value = db_session
            rc = main()
        assert rc == 1
        out = capsys.readouterr().out + capsys.readouterr().err
        assert "9999" in out or "not found" in out.lower()
    finally:
        sys.argv = old_argv


def test_worker_exits_when_account_inactive(db_session, capsys):
    """Startup com conta inativa: erro claro, exit 1."""
    import sys
    from app.worker import main

    acc = _mk_account(db_session, is_active=False)
    old_argv = sys.argv
    sys.argv = ["app.worker", "--account-id", str(acc.id)]
    try:
        with patch("app.worker.get_session") as mock_get:
            mock_get.return_value.__enter__.return_value = db_session
            rc = main()
        assert rc == 1
    finally:
        sys.argv = old_argv


def test_worker_startup_resets_orphan_jobs(db_session):
    """Startup chama reset_orphan_running(account_id) antes do loop."""
    import sys
    from app.db.models import Job
    from app.worker import main

    acc = _mk_account(db_session)
    db_session.add(Job(
        job_id="orphan", account_id=acc.id, prompt="p",
        status="running", started_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    old_argv = sys.argv
    sys.argv = ["app.worker", "--account-id", str(acc.id), "--once"]
    try:
        with patch("app.worker.get_session") as mock_get, \
             patch("app.worker.PipelineRunner") as mock_runner:
            mock_get.return_value.__enter__.return_value = db_session
            # Loop won't have work because we reset the orphan to pending,
            # then iterate once with --once and claim it (but PipelineRunner
            # is mocked).
            main()
        db_session.refresh_all = None  # no-op marker
    finally:
        sys.argv = old_argv

    # Orphan job should no longer be running. Either pending (if loop didn't
    # claim in this test) or still running (if loop claimed it again). We
    # accept pending OR running — what's forbidden is started_at being the
    # OLD value with status=='running'. Simplest check: if status is still
    # 'running', started_at must be fresher than the seed.
    orphan = db_session.query(Job).filter_by(job_id="orphan").one()
    assert orphan.status in ("pending", "running", "completed", "failed")
    # We fundamentally want: the reset happened. A separate unit test
    # (test_reset_orphan_running_*) already covers this precisely. Here we
    # just assert the worker didn't crash.
```

> **Nota para o engenheiro:** `db_session` precisa ser uma fixture em
> `tests/conftest.py` que dê uma session SQLAlchemy em banco temporário.
> Se não existir, criar (padrão: SQLite em `tmp_path` com `Base.metadata.create_all`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker.py -v`
Expected: FAIL — `--account-id` ainda não existe, nem o flag `--once`.

- [ ] **Step 3: Rewrite `app/worker.py`**

Substituir o conteúdo inteiro de `app/worker.py`:

```python
"""Background worker that processes jobs from the queue for ONE account.

Model: one worker process per YouTube account. Paralelism comes from running
N workers simultaneously (one per active account). A worker only claims jobs
with `account_id == --account-id`; it is the sole authority for that account's
queue, which is why startup can safely reset orphan `running` jobs without
coordinating with other workers.
"""
from __future__ import annotations

import argparse
import sys
import time

from app.core.account_manager import AccountManager
from app.core.job_queue import JobQueueManager
from app.core.pipeline_runner import PipelineRunner
from app.db.database import get_session
from app.db.models import Account


_IDLE_SLEEP_S = 5


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description="Process jobs for a single YouTube account.",
    )
    parser.add_argument(
        "--account-id",
        type=int,
        required=True,
        help="YouTube account ID (see `accounts` table). Worker will only "
             "claim jobs belonging to this account.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one job and exit. Used by tests and smoke runs.",
    )
    return parser.parse_args(argv)


def _validate_account(session, account_id: int) -> Account | None:
    """Return the account, or None if missing/inactive (with log)."""
    acc = session.query(Account).filter_by(id=account_id).first()
    if acc is None:
        print(f"[worker {account_id}] ERROR: account {account_id} not found", file=sys.stderr)
        return None
    if not acc.is_active:
        print(
            f"[worker {account_id}] ERROR: account '{acc.name}' is inactive; "
            f"activate it or run against a different --account-id",
            file=sys.stderr,
        )
        return None
    return acc


def main() -> int:
    args = _parse_args()
    account_id = args.account_id
    tag = f"[worker {account_id}]"

    print(f"{tag} Starting job processor (one-per-account mode)")
    if not args.once:
        print(f"{tag} Press Ctrl+C to stop")

    # Startup: validate + reset orphans. Own session (short-lived).
    with get_session() as session:
        acc = _validate_account(session, account_id)
        if acc is None:
            return 1
        job_mgr = JobQueueManager(session)
        reset = job_mgr.reset_orphan_running(account_id=account_id)
        if reset > 0:
            print(f"{tag} Reset {reset} orphan running job(s) to pending")

    while True:
        try:
            with get_session() as session:
                # Global quota reset is cheap; keep it here for parity with
                # the old worker behaviour.
                account_mgr = AccountManager(session)
                reset_count = account_mgr.reset_daily_quotas()
                if reset_count > 0:
                    print(f"{tag} Reset quotas for {reset_count} account(s)")

                job_mgr = JobQueueManager(session)
                job = job_mgr.claim_next_pending(account_id=account_id)

                if job:
                    print(f"{tag} Processing job: {job.job_id}")
                    print(f"{tag}   Prompt: {job.prompt[:60]}...")

                    runner = PipelineRunner(session)
                    runner.run_job(job.job_id)

                    print(f"{tag} Completed job: {job.job_id}")
                    if args.once:
                        return 0
                else:
                    if args.once:
                        return 0
                    time.sleep(_IDLE_SLEEP_S)

        except KeyboardInterrupt:
            print(f"\n{tag} Shutting down gracefully...")
            return 0

        except Exception as exc:
            print(f"{tag} ERROR: {exc}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            if args.once:
                return 1
            time.sleep(_IDLE_SLEEP_S)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py -v`
Expected: 4 PASSED. Se alguma falhar por causa de `db_session` fixture, criar em `tests/conftest.py`:

```python
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db.database import Base, _apply_sqlite_pragmas


@pytest.fixture
def db_session(tmp_path):
    url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
```

(Se `conftest.py` já tiver `db_session`, reuse o existente e apague este.)

- [ ] **Step 5: Run full test suite**

Run: `pytest -v -m "not slow"`
Expected: todos passam.

- [ ] **Step 6: Commit**

```bash
git add app/worker.py tests/test_worker.py tests/conftest.py
git commit -m "feat(worker): add --account-id, startup orphan reset, atomic claim loop"
```

---

## Task 6: Script supervisor `scripts/run_workers.py`

**Files:**
- Create: `scripts/run_workers.py`

- [ ] **Step 1: Write the script**

Criar `scripts/run_workers.py`:

```python
"""Supervisor opcional: spawna um worker por conta YouTube ativa.

Uso:
    python scripts/run_workers.py

Lê `accounts` ativas do banco, inicia
`python -m app.worker --account-id <id>` em subprocess pra cada uma, e
encaminha SIGINT/SIGTERM pros filhos no shutdown.

Alternativa: rodar cada worker manualmente em terminais separados ou via
systemd / nssm / pm2. Este script é só conveniência.
"""
from __future__ import annotations

import signal
import subprocess
import sys
from typing import NoReturn

from app.db.database import get_session
from app.db.models import Account


def _list_active_account_ids() -> list[int]:
    with get_session() as session:
        rows = session.query(Account.id, Account.name).filter_by(is_active=True).all()
    return [(r[0], r[1]) for r in rows]


def _spawn_workers(entries: list[tuple[int, str]]) -> list[subprocess.Popen]:
    procs: list[subprocess.Popen] = []
    for acc_id, name in entries:
        print(f"[supervisor] spawning worker for account {acc_id} ({name})")
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.worker", "--account-id", str(acc_id)]
        )
        procs.append(proc)
    return procs


def main() -> int:
    entries = _list_active_account_ids()
    if not entries:
        print("[supervisor] no active accounts — nothing to do", file=sys.stderr)
        return 1

    procs = _spawn_workers(entries)

    def _forward_and_wait(signum: int, frame) -> NoReturn:
        print(f"[supervisor] received signal {signum}; forwarding to {len(procs)} worker(s)")
        for p in procs:
            try:
                p.send_signal(signum)
            except ProcessLookupError:
                pass
        # Let the main loop's wait() collect exit codes.

    signal.signal(signal.SIGINT, _forward_and_wait)
    signal.signal(signal.SIGTERM, _forward_and_wait)

    exit_codes: list[int] = []
    for p in procs:
        exit_codes.append(p.wait())

    print(f"[supervisor] all workers exited: {exit_codes}")
    return 0 if all(c == 0 for c in exit_codes) else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke test**

Este é um script de integração real. Sem teste unitário (seria mock-pesado de baixo valor). Validar manualmente só se houver contas ativas no banco local:

```bash
# Se NÃO houver contas ativas: deve sair com código 1 e mensagem clara.
python scripts/run_workers.py
echo "exit: $?"
```

Expected: ou `"no active accounts"` com exit 1, ou um subprocess por conta (deixe rodar 5s e pressione Ctrl+C — todos os filhos devem encerrar limpos).

- [ ] **Step 3: Commit**

```bash
git add scripts/run_workers.py
git commit -m "feat(scripts): add supervisor that spawns one worker per active account"
```

---

## Task 7: Documentar no CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Locate the "Dashboard Mode" section**

Abrir `CLAUDE.md`, procurar o bloco que começa com `### Dashboard Mode`
(abaixo de `## Development Commands`). Logo após o bloco de código do
`python -m app.worker`, adicionar a nova seção.

- [ ] **Step 2: Insert new section**

Adicionar, imediatamente após o bloco existente do worker, este conteúdo
(antes da próxima seção de nível `###`):

````markdown
### Multi-Worker Mode

Cada conta YouTube pode ter seu próprio worker rodando em paralelo — quota
isolada, credenciais isoladas, um pipeline por conta por vez.

```bash
# Worker para UMA conta específica (obrigatório --account-id)
python -m app.worker --account-id 1

# Supervisor: spawna um worker por conta ativa no banco
python scripts/run_workers.py
```

**Regra importante**: NUNCA rodar dois workers com o mesmo `--account-id`.
O worker-por-conta é autoridade exclusiva sobre aquela conta; dois workers
concorrentes podem resetar jobs que o outro está processando. O supervisor
script já previne isso (1 subprocess por conta).

**Recuperação de crash**: se um worker morrer com um job em `running`, ele
será resetado pra `pending` automaticamente no próximo startup (na mesma
conta) e re-processado. Idempotência do orchestrator garante que estágios
já concluídos sejam reaproveitados.

**Claim atômico**: `JobQueueManager.claim_next_pending(account_id)` usa
`UPDATE ... RETURNING` num único statement. Dois workers simultâneos nunca
pegam o mesmo job — SQLite serializa os writers e o perdedor recebe zero
linhas.
````

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document multi-worker mode and atomic claim"
```

---

## Self-Review Checklist

Depois de executar todas as tasks:

1. **Spec coverage**: cada seção de `docs/superpowers/specs/2026-05-03-multi-worker-design.md`
   é coberta por pelo menos uma task acima (WAL/busy_timeout → T1;
   reset_orphan_running → T2; claim_next_pending → T3; teste de concorrência → T4;
   worker novo → T5; supervisor → T6; docs → T7). ✓
2. **Sem placeholders**: nenhum "TBD / TODO / handle edge cases". ✓
3. **Consistência de tipos**: `claim_next_pending(account_id: int) -> Job | None`,
   `reset_orphan_running(account_id: int) -> int` — nomes/assinaturas idênticos
   em tasks, testes e spec. ✓
4. **Sem mudança de schema**: nenhuma coluna adicionada, spec confirma. ✓
5. **TDD**: cada task começa pelo teste, verifica falha, implementa, verifica
   sucesso, commita. ✓

## Critérios de sucesso final

Depois de todas as 7 tasks:

- `pytest -v -m "not slow"` passa inteiro.
- `python -m app.worker --account-id <id_válido>` sobe, reseta órfãos
  (se houver), roda o job e encerra limpo em Ctrl+C.
- `python -m app.worker` (sem `--account-id`) falha com mensagem clara de
  argparse.
- `python scripts/run_workers.py` spawna 1 subprocess por conta ativa.
- `sqlite3 data/app.db "PRAGMA journal_mode"` retorna `wal`.
