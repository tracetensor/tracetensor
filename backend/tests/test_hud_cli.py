"""
CLI integration tests for HUD-compatible format.

Tests:
  1. `tracetensor run <hud-dir>` dispatches to the HUD runner.
  2. MCP capability env loads and registers capabilities correctly.
  3. MCP server starts, tool is callable via MCPClient (requires fastmcp).
  4. End-to-end CLI run of a HUD env with real OpenAI (skip if no key).

Run:
  cd backend && .venv/bin/python -m pytest tests/test_hud_cli.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

BLANK_DIR = Path(__file__).parent / "hud_compat" / "blank"
MCP_DIR = Path(__file__).parent / "hud_compat" / "mcp_example"

runner = CliRunner()


# ── CLI dispatch: HUD format detected and routed ─────────────────────


def test_cli_run_hud_detects_format(monkeypatch):
    """run() calls _run_hud when env.py is present — no Docker, no task.toml."""
    called_with = {}

    def fake_run_hud(**kwargs):
        called_with.update(kwargs)

    import app.cli.run as run_mod
    monkeypatch.setattr(run_mod, "_run_hud", fake_run_hud)

    from app.cli.main import app
    result = runner.invoke(app, ["run", str(BLANK_DIR), "-a", "openai"])
    # _run_hud raises typer.Exit(0) so runner exit_code is 0
    assert called_with.get("task_dir") == BLANK_DIR
    assert called_with.get("agent") == "openai"


def test_cli_run_native_format_unchanged(tmp_path):
    """Native task.toml format still reaches the original trial_runner path."""
    (tmp_path / "task.toml").write_text(
        '[task]\nname = "test-native"\n[environment]\ndocker_image = "alpine"\n'
    )
    (tmp_path / "instruction.md").write_text("echo hello")

    from app.cli.main import app
    # We expect it to fail (no Docker in test env) but NOT to route to _run_hud
    result = runner.invoke(app, ["run", str(tmp_path), "-a", "oracle", "--no-save"])
    # Exit 1 is fine (trial fails in test env); what matters is no "env.py" error
    assert "env.py" not in (result.output or "")


def test_format_detector_wired():
    """format_detector is importable and works in the CLI import path."""
    from app.services.format_detector import detect_format
    assert detect_format(BLANK_DIR) == "hud"


# ── MCP capability loading ────────────────────────────────────────────


def test_mcp_env_loads_template():
    from app.services.hud_adapter import load_hud_env
    result = load_hud_env(MCP_DIR)
    assert result.env.name == "mcp-example"
    assert "use_add_tool" in result.env.templates
    assert len(result.tasks) == 2


def test_mcp_env_bound_tasks():
    from app.services.hud_adapter import load_hud_env
    result = load_hud_env(MCP_DIR)
    t = result.tasks[0]
    assert t.kwargs["a"] == 3
    assert t.kwargs["b"] == 7


def test_mcp_env_prompt_generation():
    """MCP task generates a prompt mentioning the tool name."""
    import asyncio
    from app.services.hud_adapter import load_hud_env

    result = load_hud_env(MCP_DIR)
    task = result.tasks[0]

    async def _prompt():
        gen = task.fn(**task.kwargs)
        return await gen.asend(None)

    prompt = asyncio.run(_prompt())
    assert "add_numbers" in prompt
    assert "3" in prompt and "7" in prompt


def test_mcp_grader_correct():
    """Grader yields 1.0 for the correct numeric answer."""
    import asyncio
    from app.services.hud_adapter import load_hud_env

    result = load_hud_env(MCP_DIR)
    task = result.tasks[0]  # a=3, b=7 → 10

    async def _run():
        gen = task.fn(**task.kwargs)
        await gen.asend(None)
        return await gen.asend("10")

    assert float(asyncio.run(_run())) == 1.0


def test_mcp_grader_wrong():
    """Grader yields 0.0 for wrong answer."""
    import asyncio
    from app.services.hud_adapter import load_hud_env

    result = load_hud_env(MCP_DIR)
    task = result.tasks[0]

    async def _run():
        gen = task.fn(**task.kwargs)
        await gen.asend(None)
        return await gen.asend("99")

    assert float(asyncio.run(_run())) == 0.0


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("fastmcp"),
    reason="fastmcp not installed"
)
def test_mcp_server_starts_and_tool_callable():
    """MCP server starts in the init hook and the tool is callable."""
    import asyncio
    from app.services.hud_adapter import load_hud_env
    from app.capabilities.mcp_cap import MCPClient

    result = load_hud_env(MCP_DIR)
    env_obj = result.env

    async def _run():
        await env_obj.run_init_hooks()
        try:
            # Find the MCP capability
            mcp_caps = [c for c in env_obj.capabilities if "mcp" in c.protocol]
            if not mcp_caps:
                pytest.skip("MCP server did not register (fastmcp may have failed to bind)")
            cap = mcp_caps[0]
            client = await MCPClient.connect(cap)
            try:
                tools = await client.list_tools()
                tool_names = [t.name for t in tools]
                assert "add_numbers" in tool_names
                result_val = await client.call_tool("add_numbers", {"a": 5, "b": 8})
                content = result_val.content
                # Result content is a list of text blocks
                text = content[0].get("text", "") if content else ""
                assert "13" in text
            finally:
                await client.close()
        finally:
            await env_obj.run_shutdown_hooks()

    asyncio.run(_run())


# ── Docker images exist ───────────────────────────────────────────────


def test_browser_dockerfile_exists():
    p = Path(__file__).parent.parent / "docker" / "Dockerfile.browser"
    assert p.exists(), "docker/Dockerfile.browser not found"
    content = p.read_text()
    assert "9222" in content
    assert "chromium" in content.lower()


def test_desktop_dockerfile_exists():
    p = Path(__file__).parent.parent / "docker" / "Dockerfile.desktop"
    assert p.exists(), "docker/Dockerfile.desktop not found"
    content = p.read_text()
    assert "5900" in content
    assert "vnc" in content.lower()


# ── Real CLI run with OpenAI ──────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set"
)
def test_cli_run_hud_blank_openai():
    """
    Full CLI integration: `tracetensor run <blank-dir> -a openai -m gpt-4o-mini`
    Should load the env, run all 3 tasks, print results, exit 0.
    """
    from app.cli.main import app

    result = runner.invoke(app, [
        "run", str(BLANK_DIR),
        "-a", "openai",
        "-m", "gpt-4o-mini",
        "--no-save",
    ])
    print(result.output)
    if result.exception:
        import traceback
        traceback.print_exception(type(result.exception),
                                  result.exception,
                                  result.exception.__traceback__)
    # Expect exit 0 (at least one task passed)
    assert result.exit_code == 0
    assert "reward" in result.output.lower() or "passed" in result.output.lower()
