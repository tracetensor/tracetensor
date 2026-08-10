# Security Policy

TraceTensor runs untrusted agent code inside containers and scores it with
verifier scripts. Treat a public or multi-tenant deployment as a high-risk
attack surface.

## Supported versions

Security fixes are accepted against the latest published release on the default
branch. Report issues before disclosing publicly when possible.

## Reporting a vulnerability

Email the maintainers (see repository contact / `pyproject.toml` authors) with:

- A clear description of the issue and impact
- Steps to reproduce (PoC preferred)
- Affected version / commit if known

Do **not** open a public GitHub issue for unfixed vulnerabilities that enable
RCE outside the sandbox, secret exfiltration, or reward forgery.

## Threat model

| Threat | What can go wrong | Mitigations in TraceTensor |
|---|---|---|
| **Agent RCE** | Agent runs arbitrary commands in the evaluation environment | Agents run in Docker; default non-root user; CPU/memory limits; prefer `--network none` for tasks that do not need egress |
| **Container escape** | Breakout to the Docker host | Keep Docker/Podman patched; do not mount the Docker socket into task containers; run the API as a least-privilege user |
| **Reward forgery** | Agent edits tests or writes a fake reward | Isolated verifier path (separate grader container / locked `/logs/verifier`); do not trust agent-writable paths for scores |
| **Prompt / task injection** | Malicious `instruction.md` or fixtures steer the agent or exfiltrate keys | Treat task content as untrusted; do not inject host secrets into the sandbox env unless required; review third-party tasks |
| **Installed-agent egress** | Coding agents with network can call APIs and leak data | Network is opt-in per task; API keys for installed agents are passed deliberately — audit which keys you set |
| **Secret handling** | Keys in logs, exports, or git | Keys live in env / `backend/.env` (gitignored); never commit `.env`; Vault exports may contain trajectories — scrub before sharing |
| **API abuse** | Unauthenticated job spam on a public server | Set `API_TOKEN`; use rate limits; put TLS + auth in front of production |

## What Vault exports contain

Local Vault (`tracetensor vault export`, dashboard export) writes job config,
scores, trajectories, and verifier logs. These may include prompts, command
output, and (rarely) secrets the agent printed. Review exports before sharing.
Remote share / public Hub is **not** part of Phase 1.

## Operational guidance

1. Prefer local Docker on a trusted machine for Phase 1.
2. Set `API_TOKEN` whenever the API is reachable beyond localhost.
3. Keep `backend/.env` out of version control; use `.env.example` as the template.
4. Do not run evaluations that require host Docker socket mounts.
5. Assume any agent with network can exfiltrate whatever is in its environment.

## Cost estimates

Token counts and `$` estimates shown in Vault are **approximate** (from provider
usage when available). They are not a billing system and must not be used as a
security control.
