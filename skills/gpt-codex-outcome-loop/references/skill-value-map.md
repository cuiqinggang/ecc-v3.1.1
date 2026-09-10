# Practical value of the twelve candidate skills

Use this map to avoid loading or running irrelevant skills.

| Candidate skill | Practical value | Activate when | Default treatment |
| --- | --- | --- | --- |
| `find-skills` | Finds an existing capability instead of rebuilding it | A needed capability is missing or a mature route is unknown | Conditional, high value |
| `agent-browser` | Collects current web evidence or performs browser actions | The task needs current web content, a named site, or browser automation | Conditional |
| `grill-me` | Exposes ambiguity through structured questioning | Material requirements remain unclear after ordinary clarification | Usually replaced by the GPT start gate |
| `to-spec` | Converts a complex goal into stable acceptance boundaries | Work spans components, sessions, or substantial implementation | Standard/large tasks only |
| `to-tickets` | Splits a stable specification into assignable units | Several independent work items or agents must be coordinated | Large tasks only |
| `prototype` | Tests feasibility cheaply before full investment | Technical or user-experience uncertainty is high | Conditional |
| `frontend-design` | Improves interface hierarchy and visual quality | A user-facing web interface is in scope | Web/UI branch only |
| `vercel-react-best-practices` | Applies React/Next.js-specific implementation guidance | The actual stack is React or Next.js | Stack-specific only |
| `tdd` | Protects behavior with executable tests and regression checks | Logic is testable and correctness/regression risk matters | Common for code, not universal |
| `web-design-guidelines` | Audits accessibility and interaction quality | A web experience must be reviewed | Web/UI branch only |
| `improve-codebase-architecture` | Addresses evidenced structural friction | Tests or inspection show maintainability, coupling, or scaling problems | Never automatic after every build |
| `handoff` | Preserves state for later continuation | Work crosses sessions, operators, or release stages | Long-running work only |

## Minimum routes

### Simple reversible task

Success state → Codex execution → factual validation → GPT final check.

### Standard implementation

Success state → mature-route mapping → short execution contract → Codex implementation/test loop → GPT final check.

### High-judgment or costly task

Success state → evidence gathering → four-column route choice → optional prototype → bounded execution → GPT review at every material deviation → final validation and handoff.

## Anti-patterns

- Do not run all twelve skills to demonstrate completeness.
- Do not use React, frontend, or web-review skills for non-web work.
- Do not create tickets for a task that one bounded execution can finish.
- Do not refactor architecture without evidence of a structural problem.
- Do not ask GPT to approve routine details already inside an approved contract.
- Do not allow Codex to silently redefine the user's success criteria.
