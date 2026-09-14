"""bash-agent prompt contract: agent phase must not be told to run the grader script."""

from app.prompts.bash_agent import PROMPT_VERSION, SYSTEM_PROMPT


def test_prompt_version_is_v3():
    assert PROMPT_VERSION == "bash-agent-v3"


def test_agent_is_not_told_to_run_grader_test_sh_as_verify_step():
    """After the test-patch vault, /tests is empty during the agent phase.
    Telling the model to `bash /tests/test.sh` wastes turns. The verifier still
    runs that path after the agent stops."""
    assert "Verify — run 'bash /tests/test.sh'" not in SYSTEM_PROMPT
    assert "Do not run bash /tests/test.sh during your session" in SYSTEM_PROMPT
    assert "Work in /app" in SYSTEM_PROMPT or "work in /app" in SYSTEM_PROMPT.lower()
    assert "DONE" in SYSTEM_PROMPT
    assert "EXACTLY ONE raw bash command" in SYSTEM_PROMPT
