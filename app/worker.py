"""Background worker that processes jobs from the queue."""
from __future__ import annotations

import time
import sys

from app.core.pipeline_runner import PipelineRunner
from app.core.account_manager import AccountManager
from app.core.job_queue import JobQueueManager
from app.db.database import get_session


def main() -> int:
    """Main worker loop.

    Continuously polls for pending jobs and processes them.
    Resets daily quotas for accounts past their reset time.
    Sleeps briefly when queue is empty.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    print("[worker] Starting job processor...")
    print("[worker] Press Ctrl+C to stop")

    while True:
        try:
            with get_session() as session:
                # Reset quotas for accounts past reset time
                account_mgr = AccountManager(session)
                reset_count = account_mgr.reset_daily_quotas()
                if reset_count > 0:
                    print(f"[worker] Reset quotas for {reset_count} account(s)")

                # Get next pending job
                job_mgr = JobQueueManager(session)
                job = job_mgr.get_next_pending()

                if job:
                    print(f"[worker] Processing job: {job.job_id}")
                    print(f"[worker]   Prompt: {job.prompt[:60]}...")

                    # Process job
                    runner = PipelineRunner(session)
                    runner.run_job(job.job_id)

                    print(f"[worker] Completed job: {job.job_id}")
                else:
                    # No jobs available, sleep briefly
                    time.sleep(5)

        except KeyboardInterrupt:
            print("\n[worker] Shutting down gracefully...")
            return 0

        except Exception as exc:
            # Log error and continue (don't crash the worker)
            print(f"[worker] ERROR: {exc}")
            import traceback
            traceback.print_exc()

            # Back off on errors to avoid tight error loops
            time.sleep(5)

    return 0


if __name__ == "__main__":
    sys.exit(main())
