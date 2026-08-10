"""
Response builders shared by the routers.

A job's detail view and its list-row shape are used by both /examine and /vault.
They lived in examine.py, which meant vault.py imported them from a sibling
router — the coupling this package layout exists to prevent.

This is presentation only: reading a task's files off disk, formatting a
trajectory, folding in a usage rollup. Anything that decides *what* happens
belongs in app/services/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.job import Job, Trial
from app.models.task import Task
from app.schemas.common import as_job_status, as_trial_status
from app.schemas.examine import JobSummary, JobView, TrialView


def _trial_view(t: Trial) -> TrialView:
    return TrialView(
        id=t.id,
        trial_num=t.trial_num,
        status=as_trial_status(t.status),
        reward=t.reward,
        passed=t.passed,
        duration_s=t.duration_s,
        error=t.error,
        verifier_log=t.verifier_log,
        reward_payload=t.reward_payload,
        trajectory=t.trajectory or {},
    )


# How much of each captured file to keep in the log (chars). Configurable
# because it's a per-response size trade-off an operator may need to tune, not a
# constant with a right answer — see settings.CONTEXT_FILE_CAP_CHARS.


def _build_context(task_dir_str: Optional[str]) -> Optional[dict]:
    """Read the task's files at view time so the log is self-documenting:
    what the agent was asked to do, exactly what the verifier checks, the oracle
    reference, the environment, and what data files were present."""
    from app.storage.task_store import read_task_file

    if not task_dir_str:
        return None
    task_dir = Path(task_dir_str)
    if not task_dir.exists():
        return None

    def _read(rel: str) -> Optional[str]:
        txt = read_task_file(task_dir, rel)
        if txt is None:
            return None
        cap = settings.CONTEXT_FILE_CAP_CHARS
        return txt if len(txt) <= cap else txt[:cap] + "\n… (truncated)"

    # Data files staged into the room (everything in environment/ except Dockerfile).
    data_files = []
    env_dir = task_dir / "environment"
    if env_dir.exists():
        for f in sorted(env_dir.iterdir()):
            if f.is_file() and f.name != "Dockerfile":
                try:
                    data_files.append({"name": f.name, "size_bytes": f.stat().st_size})
                except OSError:
                    data_files.append({"name": f.name, "size_bytes": None})

    files = {
        "instruction.md": _read("instruction.md"),
        "task.toml": _read("task.toml"),
        "tests/test.sh": _read("tests/test.sh"),
        "solution/solve.sh": _read("solution/solve.sh"),
        "environment/Dockerfile": _read("environment/Dockerfile"),
    }
    # Drop files that don't exist so the UI only shows what's present.
    files = {k: v for k, v in files.items() if v is not None}

    return {
        "instruction": _read("instruction.md"),  # the "what is this about" summary
        "verifier": _read("tests/test.sh"),  # exactly what the test checks
        "oracle": _read("solution/solve.sh"),  # reference solution (if any)
        "data_files": data_files,  # what data the room was seeded with
        "files": files,  # full file bodies for the log
    }


def _job_summary(job: Job, task_name: Optional[str]) -> JobSummary:
    """One row of the jobs list, with its LLM-usage rollup folded in."""
    from app.services.vault import rollup_usage, summary_usage_fields

    fields = summary_usage_fields(rollup_usage(job.trials or []))
    # Wall clock beats summed trial time once the job has finished: trials run
    # concurrently, so the sum overstates how long the job actually took.
    if job.finished_at and job.created_at:
        fields["duration_s"] = round((job.finished_at - job.created_at).total_seconds(), 3)
    return JobSummary(
        id=job.id,
        task_id=job.task_id,
        dataset_run_id=job.dataset_run_id,
        agent=job.agent,
        model=job.model,
        task_name=task_name,
        status=as_job_status(job.status),
        n_trials=job.n_trials,
        trials_passed=job.trials_passed,
        pass_rate=job.pass_rate,
        created_at=job.created_at,
        **fields,
    )


async def _job_view(db: AsyncSession, job: Job) -> JobView:
    trials = (
        (await db.execute(select(Trial).where(Trial.job_id == job.id).order_by(Trial.trial_num)))
        .scalars()
        .all()
    )
    task = (await db.execute(select(Task).where(Task.id == job.task_id))).scalar_one_or_none()
    return JobView(
        id=job.id,
        task_id=job.task_id,
        task_name=task.name if task else None,
        agent=job.agent,
        model=job.model,
        n_trials=job.n_trials,
        status=as_job_status(job.status),
        trials_completed=job.trials_completed,
        trials_passed=job.trials_passed,
        pass_rate=job.pass_rate,
        error=job.error,
        created_at=job.created_at,
        finished_at=job.finished_at,
        context=_build_context(task.task_dir if task else None),
        trials=[_trial_view(t) for t in trials],
    )
