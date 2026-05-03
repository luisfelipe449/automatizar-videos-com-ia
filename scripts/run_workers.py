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

from app.db.database import get_session
from app.db.models import Account


def _list_active_accounts() -> list[tuple[int, str]]:
    """Return [(id, name), ...] for all active accounts."""
    with get_session() as session:
        rows = (
            session.query(Account.id, Account.name)
            .filter_by(is_active=True)
            .all()
        )
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
    entries = _list_active_accounts()
    if not entries:
        print("[supervisor] no active accounts — nothing to do", file=sys.stderr)
        return 1

    procs = _spawn_workers(entries)

    def _forward(signum: int, frame) -> None:
        print(
            f"[supervisor] received signal {signum}; "
            f"forwarding to {len(procs)} worker(s)"
        )
        for p in procs:
            try:
                p.send_signal(signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)

    exit_codes: list[int] = []
    for p in procs:
        exit_codes.append(p.wait())

    print(f"[supervisor] all workers exited: {exit_codes}")
    return 0 if all(c == 0 for c in exit_codes) else 1


if __name__ == "__main__":
    sys.exit(main())
