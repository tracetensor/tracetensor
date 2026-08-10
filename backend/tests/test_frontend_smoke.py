"""
Browser smoke tests — the dashboard actually works against a real server.

`test_frontend_contract.py` proves the frontend calls endpoints that exist and
consumes them in the right shape. That's static. This proves the flows a user
actually performs still render: register a task, run it, see a result, browse
the vault, read a leaderboard.

Between them they close the gap that made every backend score flattering — the
UI was the one part of the system with no test at all, while being the live
client of 25 endpoints.

Needs Chromium (`playwright install chromium`) and Docker for the run itself, so
it's marked and skipped when either is missing. Everything up to starting a job
works without Docker; only the trial execution needs it.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed — `pip install playwright`"
)
sync_playwright = playwright_api.sync_playwright


def _chromium_available() -> bool:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


# Every test here needs a browser; the ones that also run a container carry the
# `docker` marker on top, so `-m "not browser"` and `-m "not docker"` both work.
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not _chromium_available(),
        reason="chromium not installed — run `playwright install chromium`",
    ),
]


@pytest.fixture(scope="module")
def server():
    """A real uvicorn process serving the API and the dashboard.

    Deliberately not TestClient: the point is to exercise the actual browser
    against the actual static file, including the same-origin API base the
    frontend computes from `location`.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    tmp = Path(tempfile.mkdtemp(prefix="tt_smoke_"))
    (tmp / "tasks").mkdir(parents=True)
    import os

    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp / 'smoke.db'}",
        "TASKS_ROOT": str(tmp / "tasks"),
        "RATE_LIMIT_PER_MINUTE": "0",  # the suite makes bursts of requests
        # Explicit, because conftest sets WORKER_EMBEDDED=false for the pytest
        # process and this subprocess inherits its environment. Without it the
        # server has no worker, the job stays queued forever, and the full-run
        # test waits out its 600s timeout instead of failing with a reason.
        "WORKER_EMBEDDED": "true",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(BACKEND),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(f"{base}/api/health", timeout=5)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("server did not come up")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def page(server):
    """A browser page with console errors and failed requests recorded.

    Collecting both is what makes this suite catch the silent failures it exists
    for: a list that renders empty because the response shape changed produces no
    visible error, but it does produce a console exception or a 4xx.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        pg = ctx.new_page()
        pg.console_errors = []  # type: ignore[attr-defined]
        pg.failed_requests = []  # type: ignore[attr-defined]
        pg.on("console", lambda m: m.type == "error" and pg.console_errors.append(m.text))
        pg.on("pageerror", lambda e: pg.console_errors.append(str(e)))
        pg.on(
            "response",
            lambda r: r.status >= 400 and pg.failed_requests.append(f"{r.status} {r.url}"),
        )
        pg.goto(server, wait_until="networkidle")
        yield pg
        browser.close()


def assert_clean(page):
    """No console errors and no failed requests — the two signals that a silent
    UI failure leaves behind."""
    assert not page.console_errors, f"console errors: {page.console_errors}"
    assert not page.failed_requests, f"failed requests: {page.failed_requests}"


class TestShellLoads:
    def test_the_page_renders_without_errors(self, page):
        assert page.title()
        assert_clean(page)

    def test_the_sidebar_reaches_the_api(self, page):
        """The empty state and the can't-reach-the-API state look similar; only
        the absence of a failed request tells them apart."""
        page.wait_for_selector('[data-testid="task-list"]')
        assert "Can't reach the API" not in page.inner_text('[data-testid="task-list"]')
        assert_clean(page)

    def test_navigation_between_sections_works(self, page):
        for nav in ("#navDatasets", "#navVault", "#navTasks"):
            page.click(nav)
            page.wait_for_timeout(400)
        assert_clean(page)


class TestTaskRegistration:
    def test_registering_the_example_task_shows_it_in_the_sidebar(self, page):
        page.click('[data-testid="try-example-task"]')
        page.wait_for_selector('[data-testid="task-row"]', timeout=20_000)
        assert page.locator('[data-testid="task-row"]').count() >= 1
        assert_clean(page)

    def test_the_task_detail_view_opens(self, page):
        page.click('[data-testid="try-example-task"]')
        page.wait_for_selector('[data-testid="task-row"]', timeout=20_000)
        page.click('[data-testid="task-row"]')
        page.wait_for_selector("#examRunBtn", timeout=10_000)
        assert_clean(page)

    def test_the_agent_picker_is_populated(self, page):
        """Comes from /examine/providers. If that response shape changes, the
        dropdown silently empties and no run can be started."""
        page.click('[data-testid="try-example-task"]')
        page.wait_for_selector('[data-testid="task-row"]', timeout=20_000)
        page.click('[data-testid="task-row"]')
        page.wait_for_selector("#examAgent", timeout=10_000)
        assert page.locator("#examAgent option").count() >= 1
        assert_clean(page)


class TestVaultAndDatasets:
    def test_the_vault_list_loads(self, page):
        """Consumes a paged endpoint — the exact call that broke when list
        responses gained an envelope."""
        page.click("#navVault")
        page.wait_for_selector('[data-testid="vault-list"]', timeout=10_000)
        page.wait_for_timeout(600)
        assert_clean(page)

    def test_registering_the_example_dataset_works(self, page):
        page.click("#navDatasets")
        page.wait_for_selector('[data-testid="try-example-dataset"]', timeout=10_000)
        page.click('[data-testid="try-example-dataset"]')
        page.wait_for_selector('[data-testid="leaderboard"]', timeout=60_000)
        assert_clean(page)

    def test_the_leaderboard_renders_for_a_dataset_with_no_runs(self, page):
        """An empty leaderboard is a normal state, not an error — it must not
        throw or show a broken table."""
        page.click("#navDatasets")
        page.wait_for_selector('[data-testid="try-example-dataset"]', timeout=10_000)
        page.click('[data-testid="try-example-dataset"]')
        page.wait_for_selector('[data-testid="leaderboard"]', timeout=60_000)
        page.wait_for_timeout(800)
        assert_clean(page)


@pytest.mark.docker
class TestFullRun:
    """The whole loop, with a real container. The oracle agent runs the task's
    own solve.sh, so this costs nothing and needs no API key."""

    def test_running_a_task_streams_progress_and_shows_a_result(self, page):
        if not _docker_available():
            pytest.skip("Docker is not available")

        # Register the example task and let the app navigate to it. Deliberately
        # NOT clicking the first sidebar row: the server fixture is module-scoped,
        # so by this point earlier tests have registered the example dataset too
        # and "first row" is whichever task sorts newest. `loadExampleTask()`
        # routes to the task it just created, which is the one we mean.
        page.click('[data-testid="try-example-task"]')
        page.wait_for_selector("#examAgent", timeout=20_000)
        assert "/task/" in page.url, f"did not navigate to the task view: {page.url}"

        page.select_option("#examAgent", "oracle")
        page.fill("#examTrials", "1")
        page.click('[data-testid="run-examination"]')

        # Two panels, and the distinction matters: `exam-result` carries the live
        # SSE checklist while the run is in flight; `exam-details` is filled in
        # afterwards from the final job view. The finished result is the latter.
        page.wait_for_selector('[data-testid="exam-result"] .exam-status', timeout=30_000)

        page.wait_for_function(
            """() => {
                const el = document.querySelector('[data-testid="exam-details"]');
                return el && /COMPLETE|FAILED/.test(el.innerText);
            }""",
            timeout=600_000,
        )
        details = page.inner_text('[data-testid="exam-details"]')
        assert "COMPLETE" in details, f"job did not complete: {details[:300]}"
        # The oracle runs the task's own solve.sh, so anything but a pass means
        # the pipeline is broken, not the agent.
        assert "1/1" in details and "100%" in details, f"oracle should pass: {details[:300]}"
        assert_clean(page)
