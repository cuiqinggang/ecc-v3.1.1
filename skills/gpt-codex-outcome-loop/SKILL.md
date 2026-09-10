---
name: gpt-codex-outcome-loop
description: Coordinate GPT judgment with Codex execution through the sealed playwright-native-ai-browser bridge for multi-step building, research-to-implementation, troubleshooting, and other tasks that need an evidence-backed correction loop. Use when success criteria, route selection, or final semantic quality require more than direct execution; skip for simple factual answers or one-step edits.
---

# GPT–Codex Success-State Loop

Use a six-stage adaptive loop. Do not mechanically run every available skill.

## Roles

- **GPT control plane:** define the real goal, choose among materially different routes, resolve ambiguity, approve risky deviations, and judge whether the final outcome satisfies the user.
- **Codex execution plane:** inspect the real environment, reuse mature capabilities, edit or create artifacts, run tools and tests, collect evidence, and package delivery.
- **Tests and tools:** report observable facts. They do not decide whether the user's broader intent has been met.

Codex may make ordinary implementation decisions autonomously. Escalate only decisions that can materially change the goal, quality bar, cost, risk, or user-visible result.

## Choose the smallest mode

| Mode | Use when | Required GPT gates |
| --- | --- | --- |
| Fast | Reversible, well-specified, one or two operations | Start and final |
| Standard | Several steps, multiple plausible methods, or meaningful validation | Start, route choice, final |
| High-judgment | Ambiguous creative quality, costly execution, high risk, or conflicting evidence | Start, route choice, deviation, final |

## Six-stage workflow

### 1. Define the success state — GPT

Write a compact task contract containing:

- one outcome;
- observable acceptance criteria;
- scope and protected boundaries;
- cost or time ceiling when relevant;
- stop conditions;
- selected mode.

Do not begin a costly or destructive execution while a material ambiguity remains.

### 2. Map a mature route — GPT directs, Codex investigates

Inspect existing project capabilities first. Search for established skills, official tools, proven scripts, or maintained open-source implementations only when needed.

For a non-trivial choice, compare candidates using exactly these columns:

| Current mature capability | Actual gap | Cause of gap | Mature capability to reuse |
| --- | --- | --- | --- |

Prefer the least costly route that can meet the acceptance criteria. Do not add a new framework when an existing capability already covers the task.

### 3. Form the execution contract — GPT approves

Codex proposes a short executable plan that names:

- the actor responsible for each step;
- inputs and outputs;
- the evidence each step must produce;
- rollback or recovery for material changes;
- the next GPT decision gate.

Use tickets, a formal specification, or a prototype only when the task's size or uncertainty justifies them.

### 4. Execute in bounded batches — Codex

Perform the smallest useful batch, preserve unrelated user work, and verify actual outputs rather than claiming completion from command success alone.

Continue without GPT review for routine implementation details inside the approved contract. Return to GPT when:

- evidence contradicts the chosen route;
- a new decision changes scope, quality, cost, or risk;
- the agreed stop condition is reached;
- user-visible quality requires semantic or aesthetic judgment.

### 5. Validate and correct — tools provide facts, GPT judges

Codex runs relevant tests and presents concise evidence. GPT compares that evidence with the original success state.

- If it passes, continue to delivery.
- If it fails but remains within scope, GPT states the precise gap and sends a corrected execution contract to Codex.
- If the route is invalid or a stop condition is reached, stop and request direction rather than looping indefinitely.

### 6. Deliver and preserve — Codex prepares, GPT signs off

Deliver the requested artifact plus:

- acceptance result: `PASS`, `PARTIAL`, or `BLOCKED`;
- evidence supporting that result;
- material limitations;
- recovery or continuation information when needed.

Use a handoff document only for long-running, multi-session, or multi-operator work.

## GPT review bridge

In an integrated ChatGPT/Codex environment, perform the GPT gate in the current conversation. From a standalone Codex session, use the already installed and sealed `playwright-native-ai-browser` as the only GPT communication bridge. Read [references/playwright-gpt-bridge.md](references/playwright-gpt-bridge.md) before the first external GPT gate.

The bridge sends this packet:

```text
GPT_REVIEW_PACKET
task:
success_state:
current_stage:
evidence:
decision_needed:
options_and_tradeoffs:
codex_recommendation:
next_action_if_approved:
stop_condition:
```

Do not pretend an external GPT review occurred when no valid GPT response was received.

## Conditional skill routing

The original twelve skills are a toolbox, not twelve mandatory steps. Read [references/skill-value-map.md](references/skill-value-map.md) when deciding which of them to activate.

## Completion rule

Technical execution is not completion. Complete only when the evidence satisfies the task contract and the required GPT gate accepts the user-visible outcome.
