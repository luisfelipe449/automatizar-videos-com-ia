"""Worker startup + loop behavior."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.db.database import Base, get_engine, get_session
from app.db.models import Account, Job


@pytest.fixture
def test_db(tmp_path):
    """Create a file-backed test database engine (mirrors test_job_queue.py)."""
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def _make_account(engine, *, is_active=True, name="A") -> int:
    """Seed an account; return its id."""
    with get_session(engine) as session:
        acc = Account(
            name=name, email="a@x", client_id="ca", client_secret="sa",
            refresh_token="ra",
            quota_reset_at=datetime.now(timezone.utc),
            is_active=is_active,
        )
        session.add(acc)
        session.commit()
        return acc.id


def _patched_get_session(engine):
    """Return a drop-in context-manager that mirrors get_session(engine)."""
    @contextmanager
    def fake():
        with get_session(engine) as s:
            yield s
    return fake


def test_worker_requires_account_id(monkeypatch, capsys):
    """Rodar sem --account-id deve falhar com exit 2 (argparse default)."""
    from app import worker
    monkeypatch.setattr("sys.argv", ["app.worker"])
    with pytest.raises(SystemExit) as exc_info:
        worker.main()
    assert exc_info.value.code == 2


def test_worker_exits_when_account_not_found(test_db, monkeypatch, capsys):
    """Startup com --account-id inexistente: exit 1 + msg em stderr."""
    from app import worker
    monkeypatch.setattr("sys.argv", ["app.worker", "--account-id", "9999"])
    monkeypatch.setattr(worker, "get_session", _patched_get_session(test_db))
    rc = worker.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "9999" in err or "not found" in err.lower()


def test_worker_exits_when_account_inactive(test_db, monkeypatch, capsys):
    """Conta inativa: exit 1."""
    from app import worker
    acc_id = _make_account(test_db, is_active=False)
    monkeypatch.setattr("sys.argv", ["app.worker", "--account-id", str(acc_id)])
    monkeypatch.setattr(worker, "get_session", _patched_get_session(test_db))
    rc = worker.main()
    assert rc == 1


def test_worker_once_mode_resets_orphans_and_claims_job(test_db, monkeypatch):
    """Em --once: startup reseta orphan → loop claima o mesmo job → PipelineRunner é chamado."""
    from app import worker

    acc_id = _make_account(test_db)

    # Seed: a job stuck in 'running' (orphan from a previous crash).
    with get_session(test_db) as session:
        session.add(Job(
            job_id="orphan-1",
            account_id=acc_id,
            prompt="test prompt",
            status="running",
            started_at=datetime.now(timezone.utc),
        ))
        session.commit()

    monkeypatch.setattr("sys.argv", ["app.worker", "--account-id", str(acc_id), "--once"])
    monkeypatch.setattr(worker, "get_session", _patched_get_session(test_db))

    # Intercept PipelineRunner so we don't actually run the pipeline.
    with patch.object(worker, "PipelineRunner") as mock_runner_cls:
        rc = worker.main()

    assert rc == 0
    mock_runner_cls.assert_called_once()  # PipelineRunner(session) constructed
    mock_runner_cls.return_value.run_job.assert_called_once_with("orphan-1")

    # After --once, the job should have been claimed (status=running, started_at fresh).
    with get_session(test_db) as session:
        job = session.query(Job).filter_by(job_id="orphan-1").one()
        assert job.status == "running"  # claimed by worker loop


def test_worker_once_mode_idle_returns_zero(test_db, monkeypatch):
    """Em --once sem nenhum job pendente: sai com 0."""
    from app import worker

    acc_id = _make_account(test_db)

    monkeypatch.setattr("sys.argv", ["app.worker", "--account-id", str(acc_id), "--once"])
    monkeypatch.setattr(worker, "get_session", _patched_get_session(test_db))

    with patch.object(worker, "PipelineRunner") as mock_runner_cls:
        rc = worker.main()

    assert rc == 0
    mock_runner_cls.assert_not_called()  # no job → no runner
