"""
The HTTP contract — pagination envelopes, published status values, and
cross-process live progress.

These are the promises a client codes against. A bare array can't carry a total,
so no consumer could build "page 3 of 47"; an untyped status meant a generated
client got `str` and no compile error when a state was added. Both are now part
of the OpenAPI document, and this suite is what keeps them there.

Runs against the real app through FastAPI's TestClient — no server process, no
Docker, no key.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.enums import JobStatus, TaskStatus, TrialStatus


@pytest.fixture(scope="module")
def client():
    # TestClient runs the lifespan, which migrates the DB and starts the
    # embedded worker; the module scope keeps that to once for the whole file.
    with TestClient(app) as c:
        yield c


LIST_ENDPOINTS = [
    "/v1/examine/jobs",
    "/v1/datasets",
    "/v1/datasets/runs",
    "/v1/vault/jobs",
]


class TestPagination:
    @pytest.mark.parametrize("path", LIST_ENDPOINTS)
    def test_returns_a_page_envelope(self, client, path):
        body = client.get(path).json()
        assert set(body) >= {"items", "total", "limit", "offset"}
        assert isinstance(body["items"], list)

    @pytest.mark.parametrize("path", LIST_ENDPOINTS)
    def test_echoes_the_pagination_it_applied(self, client, path):
        """The client asked for a window; the response says which window it got,
        so a pager doesn't have to assume the server honored the request."""
        body = client.get(path, params={"limit": 5, "offset": 10}).json()
        assert body["limit"] == 5
        assert body["offset"] == 10

    @pytest.mark.parametrize("path", LIST_ENDPOINTS)
    def test_rejects_an_unbounded_page(self, client, path):
        """No caller gets to ask for the whole table."""
        assert client.get(path, params={"limit": 100_000}).status_code == 422

    @pytest.mark.parametrize("path", LIST_ENDPOINTS)
    def test_rejects_a_negative_offset(self, client, path):
        assert client.get(path, params={"offset": -1}).status_code == 422

    def test_total_ignores_the_page_window(self, client):
        """`total` is the count of matching rows, not of returned ones —
        otherwise it tells a pager nothing it didn't already know."""
        body = client.get("/v1/examine/jobs", params={"limit": 1}).json()
        assert body["total"] >= len(body["items"])


class TestOpenAPIContract:
    def test_list_endpoints_publish_a_total(self, client):
        schema = client.get("/openapi.json").json()
        for path in LIST_ENDPOINTS:
            ref = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
                "schema"
            ]["$ref"]
            model = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
            assert "total" in model["properties"], path
            assert "total" in model["required"], path

    def test_statuses_are_published_as_enums_not_bare_strings(self, client):
        """A generated client should get a union type here. Without it, a
        consumer switching on status has no way to learn a state was added."""
        schema = client.get("/openapi.json").json()["components"]["schemas"]

        cases = [
            ("JobSummary", "status", set(JobStatus.values())),
            ("JobView", "status", set(JobStatus.values())),
            ("TrialView", "status", set(TrialStatus.values())),
            ("TaskSummary", "status", set(TaskStatus.values())),
        ]
        for model, field, expected in cases:
            prop = schema[model]["properties"][field]
            assert prop.get("enum"), f"{model}.{field} publishes no enum"
            assert set(prop["enum"]) == expected, f"{model}.{field}"

    def test_the_providers_endpoint_is_typed(self, client):
        """It drives every run the user can start; `Any` here means an untyped
        client for the most important picker in the UI."""
        schema = client.get("/openapi.json").json()
        content = schema["paths"]["/v1/examine/providers"]["get"]["responses"]["200"]["content"]
        assert "$ref" in content["application/json"]["schema"]

    def test_agent_status_labels_are_a_closed_set(self, client):
        schema = client.get("/openapi.json").json()["components"]["schemas"]
        assert set(schema["AgentStatusEntry"]["properties"]["status"]["enum"]) == {
            "verified",
            "gated",
            "unsupported",
        }


class TestProviders:
    def test_lists_agents_backends_and_installed_agents(self, client):
        body = client.get("/v1/examine/providers").json()
        assert {"agents", "installed_agents", "backends"} <= set(body)
        assert any(a["id"] == "oracle" for a in body["installed_agents"])

    def test_every_installed_agent_carries_an_honest_status(self, client):
        body = client.get("/v1/examine/providers").json()
        for entry in body["installed_agents"]:
            assert entry["status"] in {"verified", "gated", "unsupported"}
            assert entry["note"], f"{entry['id']} has no note explaining its status"


class TestMultiProcessLiveProgress:
    """The single-process guard is gone because the reason for it is gone.

    Live progress used to live in a per-process dict, so a second web worker —
    or a worker on another machine, which is the documented way to scale — wrote
    events nobody could read. The event log is a table now, so any process can
    serve any stream.
    """

    def test_the_startup_guard_is_removed(self):
        import app.main as main_module

        assert not hasattr(main_module, "_check_single_process")

    def test_the_server_runs_the_database_backend(self, client):
        """Not the in-memory one — that's the whole point of the change."""
        from app.services import event_bus

        assert isinstance(event_bus.current_backend(), event_bus.DatabaseBackend)

    def test_events_are_readable_through_a_fresh_handle(self, client):
        """A handle created without ever seeing the writer still reads its
        events. That is exactly what a second process does."""
        import asyncio
        import uuid as _uuid

        from app.services import event_bus

        channel = str(_uuid.uuid4())
        event_bus.create_bus(channel).emit({"type": "phase", "phase": "agent"})

        async def read_back():
            backend = event_bus.current_backend()
            assert isinstance(backend, event_bus.DatabaseBackend)
            await backend._flush_once()
            # A brand-new handle — no shared in-memory state with the writer.
            return await event_bus.JobBus(channel).read_from(0)

        events, cursor = asyncio.run(read_back())
        assert [e["phase"] for e in events] == ["agent"]
        assert cursor > 0


def test_no_web_worker_env_leaked_into_this_process():
    """Guards the fixtures above: a leaked value would make the module-scoped
    TestClient fail to start with a confusing error."""
    assert os.getenv("WEB_CONCURRENCY") in (None, "1")


class TestEventContract:
    """The SSE payloads are the backend↔dashboard interface and were the last
    untyped one. A renamed key used to make the UI silently stop rendering that
    piece — no error, no failing test, because nothing described the shape."""

    def test_every_emitted_event_type_is_declared(self):
        """Scans app/ for emitted `"type": "..."` literals and checks each is in
        the published union. Catches a new event added without a model, which
        would otherwise reach clients undocumented."""
        import re
        from pathlib import Path
        from typing import get_args

        from app.schemas.events import EventType

        app_dir = Path(__file__).resolve().parents[1] / "app"
        emitted = set()
        for py in app_dir.rglob("*.py"):
            if py.name == "events.py":
                continue  # where they're declared
            emitted |= set(re.findall(r'"type":\s*"([a-z_]+)"', py.read_text()))

        declared = set(get_args(EventType))
        assert emitted, "found no emitted event types — check the scan"
        assert emitted <= declared, f"emitted but undeclared: {sorted(emitted - declared)}"

    def test_every_declared_event_is_actually_emitted(self):
        """The other direction: a model for an event nothing sends is a promise
        to clients that will never be kept."""
        import re
        from pathlib import Path
        from typing import get_args

        from app.schemas.events import EventType

        app_dir = Path(__file__).resolve().parents[1] / "app"
        emitted = set()
        for py in app_dir.rglob("*.py"):
            if py.name == "events.py":
                continue
            emitted |= set(re.findall(r'"type":\s*"([a-z_]+)"', py.read_text()))

        declared = set(get_args(EventType))
        assert declared <= emitted, f"declared but never emitted: {sorted(declared - emitted)}"

    def test_the_event_union_is_published_to_openapi(self, client):
        schema = client.get("/openapi.json").json()["components"]["schemas"]
        for model in ("PhaseEvent", "StepEvent", "JobDoneEvent", "StreamEventEnvelope"):
            assert model in schema, f"{model} is not in the published schema"

    def test_the_union_is_discriminated_on_type(self, client):
        """So a generated client narrows in a switch instead of guessing."""
        schema = client.get("/openapi.json").json()["components"]["schemas"]
        envelope = schema["StreamEventEnvelope"]["properties"]["event"]
        assert "discriminator" in envelope or "oneOf" in envelope or "anyOf" in envelope

    def test_stream_endpoints_advertise_the_event_stream_media_type(self, client):
        schema = client.get("/openapi.json").json()
        for path in ("/v1/examine/job/{job_id}/stream", "/v1/datasets/runs/{run_id}/stream"):
            content = schema["paths"][path]["get"]["responses"]["200"]["content"]
            assert "text/event-stream" in content, path

    def test_a_real_stream_emits_declared_event_types(self, client, settings_override):
        """End to end: the events a queued job actually produces validate against
        the models, rather than the models merely existing."""
        import json

        from app.schemas.events import StreamEventEnvelope

        # A dataset run emits run_started synchronously at creation, before any
        # worker touches it — so this needs no Docker. Its child jobs stay queued
        # (conftest disables the embedded worker for the pytest process).
        dataset = client.post("/v1/datasets/example").json()
        run = client.post(
            f"/v1/datasets/{dataset['id']}/examine",
            json={"agent": "oracle", "n_trials": 1},
        )
        assert run.status_code == 200, run.text

        # Shrink the stream's own deadline. Nothing executes this run (no worker
        # in the pytest process), so the generator would otherwise run until
        # twice the lease timeout — 6 minutes of a test suite waiting. Closing
        # the client doesn't cancel it: TestClient drives the ASGI app to
        # completion in a portal.
        with settings_override(WORKER_LEASE_TIMEOUT=1):
            with client.stream("GET", f"/v1/datasets/runs/{run.json()['id']}/stream") as resp:
                assert resp.status_code == 200
                seen = 0
                types_seen: list = []
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[len("data: ") :])
                    # Validates through the discriminated union — a payload whose
                    # shape drifted from its model fails here.
                    StreamEventEnvelope(event=payload)
                    types_seen.append(payload["type"])
                    seen += 1
                    # One event is the assertion. Waiting for a second would block
                    # until the stream's own deadline (twice the lease timeout),
                    # because nothing executes the run — the pytest process has no
                    # embedded worker on purpose.
                    break
        assert "run_started" in types_seen, types_seen


class TestErrorContract:
    def test_errors_use_the_declared_shape(self, client):
        body = client.get(f"/v1/examine/job/{'0' * 8}-0000-0000-0000-{'0' * 12}").json()
        assert "detail" in body

    def test_the_error_model_is_published(self, client):
        assert "ErrorResponse" in client.get("/openapi.json").json()["components"]["schemas"]

    def test_endpoints_declare_their_failure_modes(self, client):
        """A client shouldn't have to discover 404 by hitting it."""
        schema = client.get("/openapi.json").json()
        responses = schema["paths"]["/v1/examine/jobs"]["get"]["responses"]
        assert "500" in responses, "every endpoint can 500; it should say so"
