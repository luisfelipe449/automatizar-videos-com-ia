"""Background worker that processes jobs from the queue for ONE account.

Model: one worker process per YouTube account. Parallelism comes from running
N workers simultaneously (one per active account). A worker only claims jobs
with `account_id == --account-id`; it is the sole authority for that
account's queue, which is why startup can safely reset orphan `running` jobs
without coordinating with other workers.
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
    """Return the account, or None if missing/inactive (with log to stderr)."""
    acc = session.query(Account).filter_by(id=account_id).first()
    if acc is None:
        print(
            f"[worker {account_id}] ERROR: account {account_id} not found",
            file=sys.stderr,
        )
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

    # Startup: validate + reset orphans. Short-lived session.
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
                # Global quota reset is cheap; keep here for parity with old
                # single-process worker behaviour.
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
