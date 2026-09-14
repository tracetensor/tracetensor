"""
Pydantic models describing an evaluation task.

Doctor analogy: this is the *Patient Chart* — the structured record of who
the patient (agent task) is, what environment (room) it needs, and how long
the examination and health-check may run.

The shapes here follow the standard task.toml layout so that an existing
task validates and parses with zero changes.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Author(BaseModel):
    name: str
    email: Optional[str] = None


class VerifierConfig(BaseModel):
    """How the health check (test) runs."""

    timeout_sec: float = 120.0
    # "script"    -> run tests/test.sh and read the reward (default).
    # "llm-judge" -> grade the agent's output with a model against `rubric`.
    type: str = "script"
    # "shared" -> verifier runs in the agent's environment.
    # "separate" -> verifier runs in its own container.
    environment_mode: str = "shared"
    network_mode: Optional[str] = None  # phase override
    allowed_hosts: list[str] = Field(default_factory=list)
    env: dict = Field(default_factory=dict)  # verifier-specific env vars
    user: Optional[str] = None
    # LLM-judge settings (used when type = "llm-judge").
    rubric: Optional[str] = None  # inline rubric text
    rubric_file: Optional[str] = None  # or a rubric file under tests/
    judge_model: Optional[str] = None  # provider/model, e.g. "openai/gpt-4.1-mini"
    judge_output_path: Optional[str] = None  # container file to grade (else agent stdout)

    @field_validator("environment_mode")
    @classmethod
    def _valid_env_mode(cls, v: str) -> str:
        if v not in ("shared", "separate"):
            raise ValueError("verifier.environment_mode must be 'shared' or 'separate'")
        return v

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in ("script", "llm-judge"):
            raise ValueError("verifier.type must be 'script' or 'llm-judge'")
        return v


class AgentConfig(BaseModel):
    """How the patient (agent) is allowed to work."""

    timeout_sec: Optional[float] = 120.0
    # Dedicated budget for the agent install step (e.g. pip install, curl | bash).
    # Consumed BEFORE the main agent_timeout clock starts, so install time does not
    # eat into the agent's actual working budget. Only used by BaseInstalledAgent.
    setup_timeout_sec: float = 360.0
    network_mode: Optional[str] = None  # phase override
    allowed_hosts: list[str] = Field(default_factory=list)
    user: Optional[str] = None
    max_steps: Optional[int] = None  # per-task step budget for built-in LLM agent


class EnvironmentConfig(BaseModel):
    """The examination room the agent works inside."""

    build_timeout_sec: float = 600.0
    # Separate cap for container-start + post-start execs (docker run + mkdir/chmod).
    # The build phase has its own build_timeout_sec; this guards the start phase.
    setup_timeout_sec: float = 60.0
    network_mode: str = "public"  # public | no-network | allowlist
    allowed_hosts: list[str] = Field(default_factory=list)
    docker_image: Optional[str] = None
    # Docker --platform (e.g. "linux/amd64"). None = host default. Lets an x86-only
    # image (SWE-Bench ships linux/amd64) run on an arm64 host via emulation.
    platform: Optional[str] = None
    os: str = "linux"
    workdir: Optional[str] = None
    user: Optional[str] = None
    env: dict = Field(default_factory=dict)  # environment variables
    cpus: Optional[int] = None
    memory_mb: Optional[int] = None
    storage_mb: Optional[int] = None

    @field_validator("os")
    @classmethod
    def _valid_os(cls, v: str) -> str:
        if v not in ("linux", "windows"):
            raise ValueError("environment.os must be 'linux' or 'windows'")
        return v

    @field_validator("network_mode")
    @classmethod
    def _valid_network(cls, v: str) -> str:
        if v not in ("public", "no-network", "allowlist"):
            raise ValueError("environment.network_mode must be public | no-network | allowlist")
        return v


class StepConfig(BaseModel):
    """One step in a multi-step task.

    Each step runs the agent against its own instruction and grades it with its
    own verifier. The shared sandbox environment (container) is reused across
    steps so state persists — the agent's changes in step 1 are visible in step 2.
    The final trial reward is the mean of all step rewards.

    Layout convention (auto-detected by parse_task_toml):
        steps/
          01-first/
            instruction.md
            tests/
              test.sh
          02-second/
            instruction.md
            tests/
              test.sh
    """

    # Relative to the step directory.
    instruction_file: str = "instruction.md"
    tests_dir: str = "tests"
    agent_timeout_sec: float = 120.0
    verifier_timeout_sec: float = 120.0


class TaskConfig(BaseModel):
    """The full Patient Chart parsed from task.toml."""

    schema_version: str = "1.3"
    name: str
    description: Optional[str] = None
    authors: list[Author] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    category: Optional[str] = None
    difficulty_explanation: Optional[str] = None

    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)

    # Paths the agent produces that a SEPARATE verifier needs (copied agent→verifier).
    artifacts: list[str] = Field(default_factory=list)

    # Multi-step: when non-empty, the trial loops over steps using the shared
    # sandbox. Single-step tasks leave this empty.
    steps: list[StepConfig] = Field(default_factory=list)

    # Anything else under [metadata] we keep verbatim.
    metadata: dict = Field(default_factory=dict)
