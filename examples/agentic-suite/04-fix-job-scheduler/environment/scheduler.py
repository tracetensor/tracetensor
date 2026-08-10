"""Broken priority queue — pops lowest priority first."""


class JobQueue:
    def __init__(self) -> None:
        self._items: list[tuple[int, int, str]] = []
        self._seq = 0

    def push(self, job_id: str, priority: int) -> None:
        self._seq += 1
        self._items.append((priority, self._seq, job_id))

    def pop(self) -> str | None:
        if not self._items:
            return None
        # BUG: min instead of max priority
        idx = min(range(len(self._items)), key=lambda i: self._items[i][0])
        _, _, job_id = self._items.pop(idx)
        return job_id
