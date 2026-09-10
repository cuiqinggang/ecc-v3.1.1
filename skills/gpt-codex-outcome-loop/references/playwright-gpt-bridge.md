# Playwright-native GPT bridge

Use the mature sealed `playwright-native-ai-browser` installation. Do not recreate it, alter its network configuration, start another browser system, or substitute a generic browser.

## Bridge contract

1. Codex writes one `GPT_REVIEW_PACKET` with a unique task id and gate id.
2. Reuse the dedicated browser's existing authenticated GPT session through its formal `query` / `send` / `wait_extract` entrypoints, using the exact interface exposed by the installed skill.
3. Submit the packet once. Require an exact acknowledgement containing the same task id and gate id before treating the request as accepted.
4. Extract the complete GPT response, including long text, tables, JSON, or code blocks. Do not rely on a visible partial preview.
5. Accept the response only when it contains the matching task id, gate id, a decision, reasons, and the next permitted Codex action.
6. Save the request, acknowledgement, extracted response, and timestamps as evidence when the task needs an audit trail.
7. Codex resumes only within the approved scope. A changed goal, quality bar, cost, or risk requires a new gate.

## Response contract

Ask GPT to return:

```text
GPT_REVIEW_RESULT
task_id:
gate_id:
decision: APPROVE | REVISE | STOP | NEED_USER
reason:
approved_scope:
next_codex_action:
acceptance_delta:
```

## Reliability rules

- Reconnect through the sealed skill's supported recovery path if the page or connection is stale.
- Never submit the same gate twice unless evidence proves the first submission was not accepted.
- Do not infer completion from navigation, button state, or elapsed time; require extracted response content.
- Preserve the pending packet on timeout or interruption so the workflow can resume without rewriting the decision context.
- Keep credentials, tokens, cookies, private keys, and unrelated local content out of the packet.
- Respect the task's own watchdog. Do not retry indefinitely.

## Failure result

If the sealed bridge is missing, its authenticated session is unavailable, or a valid response cannot be extracted within the allowed recovery attempt, return:

```text
STATUS=BLOCKED_GPT_BRIDGE
TASK_ID=<task id>
GATE_ID=<gate id>
PACKET_PRESERVED=<path or evidence id>
CODEX_EXECUTION_RESUMED=false
```

Do not fall back to another browser or claim that GPT approved the decision.
