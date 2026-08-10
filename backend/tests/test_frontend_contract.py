"""
The frontend's API calls, checked against the server's actual OpenAPI schema.

`frontend/index.html` is a hand-written client of 25-odd endpoints. Nothing
verified that those paths exist, or that a list endpoint is consumed as a page
rather than an array — so a backend change could break the dashboard silently:
the UI renders an empty list, no console error, no failing test.

That nearly happened. When list responses became `{items, total, limit, offset}`
the six call sites had to switch to `apiList()`; missing one would have shown an
empty table with no indication anything was wrong.

This is a static check — it parses the URLs out of the frontend's JavaScript and
compares them to the schema the server publishes. No browser, no network. The
Playwright suite (test_frontend_smoke.py) covers behavior; this covers the
contract, and it's the cheaper of the two to keep green.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.main import app

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"

_HELPERS = ("api", "apiList", "apiPage")
_STRING = re.compile(r"""["'`]([^"'`]*)["'`]""")


def _call_argument(js: str, open_paren: int) -> str:
    """The first argument's source text, from just after `(` to the comma or
    closing paren that ends it. Scanned rather than regexed because the second
    argument is often an object literal full of braces and commas."""
    depth = 0
    for i in range(open_paren + 1, len(js)):
        ch = js[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return js[open_paren + 1 : i]
            depth -= 1
        elif ch == "," and depth == 0:
            return js[open_paren + 1 : i]
    return ""


def _url_pattern(expression: str) -> str:
    """Turn a concatenated URL expression into a comparable path pattern.

    `"/examine/task/" + id + "/jobs"` -> `/examine/task/*/jobs`

    Taking only the first literal would lose the `/jobs` suffix and make the
    call look like it targeted a different endpoint — which is exactly the kind
    of false result that would erode trust in this suite.
    """
    literals: list[str] = _STRING.findall(expression)
    if not literals:
        return ""
    if len(literals) == 1:
        # A lone literal ending in "/" still has a variable appended after it.
        return literals[0] + (
            "*"
            if expression.strip() != f'"{literals[0]}"'
            and expression.strip().rstrip() != literals[0]
            and literals[0].endswith("/")
            else ""
        )
    return "*".join(literals)


def _call_sites(js: str) -> list[tuple[str, str]]:
    """(helper_name, url_pattern) for every API call the frontend makes."""
    sites: list[tuple[str, str]] = []
    for helper in _HELPERS:
        for m in re.finditer(rf"\b{helper}\(", js):
            pattern = _url_pattern(_call_argument(js, m.end() - 1))
            if pattern.startswith("/"):
                sites.append((helper, pattern))
    # Raw fetch() and EventSource() bypass the helpers; they still have to point
    # at real endpoints. Both go through the same argument scanner so an options
    # object ({method: "POST", …}) can't leak into the parsed URL.
    for pat, label in ((r"\bfetch\(", "fetch"), (r"\bnew EventSource\(", "EventSource")):
        for m in re.finditer(pat, js):
            arg = _call_argument(js, m.end() - 1)
            if "APIV" not in arg:
                continue  # a fetch at some other host isn't ours to check
            pattern = _url_pattern(arg)
            if pattern.startswith("/"):
                sites.append((label, pattern))
    return sites


def _schema_paths(schema: dict) -> list[str]:
    """Versioned API paths only — the frontend prefixes everything with /v1."""
    return [p[len("/v1") :] for p in schema["paths"] if p.startswith("/v1/")]


def _matches(pattern: str, schema_path: str) -> bool:
    """Does a frontend URL pattern correspond to this schema path?

    `*` stands for an interpolated value and matches exactly one segment, which
    is what a path parameter is.
    """
    want = [s for s in pattern.strip("/").split("/") if s]
    have = [s for s in schema_path.strip("/").split("/") if s]
    if len(want) != len(have):
        return False
    for w, h in zip(want, have):
        if w == "*":
            # An interpolated value can only be a path parameter. Letting it
            # match a literal segment too would make `/examine/{id}` look like a
            # call to `/examine/jobs`, and every check here inherit that error.
            if not h.startswith("{"):
                return False
        elif w != h:
            return False
    return True


@pytest.fixture(scope="module")
def schema():
    with TestClient(app) as c:
        return c.get("/openapi.json").json()


@pytest.fixture(scope="module")
def frontend_js() -> str:
    return FRONTEND.read_text()


class TestEveryCallSiteExists:
    def test_the_frontend_is_where_we_think_it_is(self):
        assert FRONTEND.exists(), f"frontend not found at {FRONTEND}"

    def test_we_actually_found_call_sites(self, frontend_js):
        """Guards the regexes: if the frontend is refactored so these stop
        matching, every other test here would vacuously pass."""
        assert len(_call_sites(frontend_js)) >= 20

    def test_no_call_site_points_at_a_missing_endpoint(self, frontend_js, schema):
        paths = _schema_paths(schema)
        missing = [
            f"{helper}({prefix!r})"
            for helper, prefix in _call_sites(frontend_js)
            if not any(_matches(prefix, p) for p in paths)
        ]
        assert not missing, "frontend calls endpoints the API doesn't serve: " + ", ".join(missing)


class TestListEndpointsAreConsumedAsPages:
    """A list endpoint returns `{items, total, …}`. Calling it with plain `api()`
    yields an object where the code expects an array — `.filter`/`.map` throws or
    the UI renders nothing. This is the exact failure the Page envelope
    introduced, so it gets its own check."""

    def _page_returning_paths(self, schema) -> list[str]:
        out = []
        for path, methods in schema["paths"].items():
            if not path.startswith("/v1/"):
                continue
            get = methods.get("get")
            if not get:
                continue
            content = get["responses"].get("200", {}).get("content", {})
            ref = content.get("application/json", {}).get("schema", {}).get("$ref", "")
            model = ref.rsplit("/", 1)[-1]
            if model and "total" in schema["components"]["schemas"].get(model, {}).get(
                "properties", {}
            ):
                out.append(path[len("/v1") :])
        return out

    def test_the_schema_still_has_paged_endpoints(self, schema):
        assert self._page_returning_paths(schema), "no paged endpoints found — check the fixture"

    def test_paged_endpoints_are_never_called_with_bare_api(self, frontend_js, schema):
        paged = self._page_returning_paths(schema)
        offenders = [
            f"api({prefix!r}) — returns a page envelope, use apiList()/apiPage()"
            for helper, prefix in _call_sites(frontend_js)
            if helper == "api" and any(_matches(prefix, p) for p in paged)
        ]
        assert not offenders, "; ".join(offenders)

    def test_the_unwrapping_helpers_exist(self, frontend_js):
        assert "async function apiList(" in frontend_js
        assert "async function apiPage(" in frontend_js

    def test_apilist_tolerates_a_bare_array(self, frontend_js):
        """So the UI keeps working against a server that predates the envelope."""
        body = frontend_js.split("async function apiList(", 1)[1].split("}", 1)[0]
        assert "Array.isArray" in body


class TestStreamsUseTheTokenFallback:
    """EventSource can't set headers, so an authenticated stream has to carry
    `?token=`. Miss it and live progress silently 401s on any secured
    instance — while every other call keeps working."""

    def test_every_stream_url_goes_through_withtoken(self, frontend_js):
        for line in frontend_js.splitlines():
            if "new EventSource(" in line:
                assert "withToken(" in line, f"stream without token fallback: {line.strip()}"

    def test_the_server_accepts_that_fallback(self):
        from app.core.security import _presented_token

        class _Req:
            """The two attributes _presented_token reads. A real Request is a
            large object to build for a two-field read."""

            headers: dict = {}
            query_params = {"token": "abc"}

        assert _presented_token(cast(Request, _Req())) == "abc"
