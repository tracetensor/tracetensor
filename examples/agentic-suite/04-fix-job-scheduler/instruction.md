# Fix job scheduler — wrong job order in worker

## PR description

`/app/scheduler.py` defines `JobQueue` with `push(job_id, priority)` and
`pop()` that should return the **highest** priority job id waiting (larger
priority number wins). Ties: earlier push wins (FIFO among equal priority).

Fix `/app/scheduler.py`. Do not change tests.

After pushing `(a,1)`, `(b,3)`, `(c,2)` — pops should be `b`, `c`, `a`.
