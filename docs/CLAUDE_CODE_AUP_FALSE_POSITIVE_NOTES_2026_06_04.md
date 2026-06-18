# Claude Code AUP False-positive Notes — 2026-06-04

## Symptom

During otherwise normal Claude Code use, the CLI can fail with:

```text
API Error: Claude Code is unable to respond to this request, which appears to violate our Usage Policy
(https://www.anthropic.com/legal/aup). Please double press esc to edit your last message or start a new session for Claude Code to assist
with a different task.
```

In this project, the likely cause is not an actual request to violate the policy. The more likely
cause is that Claude Code sends a large working context to Anthropic, and that context contains
many terms and code paths that look high-risk to an automated safety classifier when separated
from the project's defensive/research context.

## Why This Repository Is Easy To Misclassify

### 1. High-risk words appear in legitimate engineering contexts

Aletheia contains many legitimate references to:

- sandboxing and sandbox limits;
- guardrails and policy gates;
- credentials, tokens, API keys, OAuth, and secrets;
- permission modes;
- prompt injection and critic robustness;
- autonomous execution and AI-authored code;
- GitHub repository creation and push workflows.

These terms are normal for this repository, but they overlap with policy-sensitive topics such
as unauthorized access, credential misuse, guardrail circumvention, malware, and platform abuse.

### 2. `bypassPermissions` is a particularly risky literal

The codebase uses Claude Code's `permission_mode="bypassPermissions"` setting in legitimate
orchestration code. The string is an official SDK/CLI mode, but the word "bypass" combined with
"permissions" can look like a request to evade controls when it appears in a broad context dump.

Relevant areas include:

- `aletheia/orchestrator/client.py`
- `aletheia/orchestrator/worker.py`

### 3. The project combines agentic automation with code execution

Aletheia's intended design includes autonomous research loops, generated code, sandboxed
execution, budget controls, external credentials, critic providers, and optional GitHub actions.
This is a legitimate research-automation architecture, but the combination is exactly the kind
of context that automated policy filters inspect carefully.

### 4. Safety review documents amplify the trigger surface

Several docs intentionally discuss weak sandbox boundaries, credential exposure, critic
independence, prompt injection, fail-closed behavior, and guardrail hardening. These are
defensive review notes, but if Claude Code includes them in context during unrelated coding work,
the classifier may treat the request as policy-sensitive.

### 5. Long sessions accumulate unsafe-looking context

Claude Code is not evaluated only on the user's latest instruction. A long session may include
previous discussion of sandbox boundaries, credentials, policy, or bypass terminology. Later
ordinary requests can inherit that context and trigger the same error.

## Working Hypothesis

Most recurring failures are likely context-level false positives:

> The user's immediate task is allowed, but the accumulated context contains enough
> policy-sensitive vocabulary and code snippets that Claude Code's upstream classifier blocks
> the request before the model can answer.

This is different from a model refusal after reasoning. The error is an API-level block.

## Mitigation Playbook

### For normal development work

- Start a fresh Claude Code session for ordinary coding tasks after any security/policy review.
- Narrow the task to specific files instead of asking Claude Code to inspect the whole repo.
- Avoid asking Claude Code to read broad docs directories unless the docs are relevant.
- Do not include `.env`, credential stores, tokens, or keychain output in context.
- Avoid phrasing ordinary tasks with high-trigger terms such as "bypass guardrails" or
  "sandbox escape"; use precise defensive wording such as "permission-mode handling",
  "execution isolation", or "policy compliance".

### For security and hardening reviews

- State clearly that the review is defensive, authorized, and limited to this local repository.
- Keep the scope narrow: name the exact files or subsystems being reviewed.
- Ask for risk identification and safe remediation, not exploit construction.
- Separate security review sessions from implementation sessions.
- Prefer summaries of findings over repeatedly pasting large code blocks containing sensitive
  trigger terms.

### For agent/orchestrator work

- When modifying Claude Code SDK settings, refer to `permission_mode` by its API name only when
  necessary.
- Avoid describing legitimate SDK modes as "bypassing security"; describe the intended behavior:
  unattended local execution, tool permission policy, or human approval mode.
- Keep credentials and provider setup docs separate from unrelated coding contexts.

## Suggested Repo-local Guardrail

Add or update a Claude-facing project instruction file, such as `CLAUDE.md`, with a concise
context note:

```md
# Claude Code Context Note

This repository is an authorized local research-automation project. References to sandboxing,
credentials, policy gates, permission modes, and guardrails are defensive engineering terms used
to isolate AI-authored code, protect secrets, and enforce compliance. Do not retrieve, print,
modify, or exfiltrate secrets. Do not assist with unauthorized access, malware, or evasion of
third-party controls. When working on security-sensitive code, provide defensive analysis and
safe remediation only.
```

This will not override Anthropic's API-level classifier, but it may help the model interpret
repository context correctly in sessions that are not blocked upstream.

## Practical Recommendation

The best operational fix is context hygiene:

1. Use short, task-specific Claude Code sessions.
2. Keep security review, credential work, and ordinary feature development in separate sessions.
3. Point Claude Code at the minimum files needed.
4. Avoid loading review docs full of policy-sensitive terms unless the task needs them.
5. Restart the session immediately after an AUP block instead of continuing with the same
   accumulated context.

For this repository, recurring AUP blocks should be treated first as a context-management problem,
not as evidence that normal development is prohibited.
