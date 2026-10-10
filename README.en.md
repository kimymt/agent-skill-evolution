# Agent Evolution

[日本語](README.md)

A local CLI for comparing the body of a shared agent skill's `SKILL.md` using fixed tasks, models, and reasoning settings. It records candidate revisions and evaluation results, supports comparisons of manually written candidates, and generates candidates with [GEPA](https://github.com/gepa-ai/gepa).

Only the skill body is eligible for evolution. Frontmatter, supporting files, and the agent's own code are outside its scope. Applying a candidate requires a review of the evaluation results and human approval.

## Release status

This distribution is based on a snapshot saved before changes on October 10, 2026, at 21:27. It does not include subsequent compatibility improvements or security fixes.

The code includes CLI integrations and event adapters for Codex, Claude Code, OpenCode, and Pi. Previous live model connection checks covered only Codex on macOS. Live connections for the other products and Docker remain unverified. The Codex native configuration requires version `0.160.0`; compatibility with the latest CLI is not guaranteed.

The code permits settings controlled by the evaluation workspace to affect secret scanning. Its skill-read detection can accept a read from another path as a read of the specified skill, and its process cleanup can miss child processes that move into a separate session. The observer installer identifies managed hooks by a path string in the command, which can cause it to remove unrelated hooks. Parsing deeply nested JSON can also raise exceptions that are not recorded as ordinary evaluation failures. These issues remain unresolved in this distribution.

## Setup

Use Python 3.11 or later. Run the following commands from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/agent-evolution --help
```

`requirements.lock` lists pinned dependency versions, including GEPA and pytest for testing. It does not record distribution file hashes. These steps do not install agent CLIs or container images.

## Offline demo

```sh
.venv/bin/agent-evolution --state .evolution-demo demo
.venv/bin/agent-evolution dataset validate --suite fixtures/bug-investigation-v1.json
```

`demo` simulates candidate creation, comparison, and report generation. It does not call a live model, and its results cannot pass the adoption criteria. The bundled dataset contains nine synthetic tasks: two for train, two for validation, and five for holdout.

## Live model comparisons

Live evaluation requires the selected agent CLI, a way to connect to the model, [Gitleaks](https://github.com/gitleaks/gitleaks), and a prepared execution backend.

Copy and configure `examples/codex-smoke.json`. Relative `skill` and `suite` paths are resolved from the configuration file's directory. The example specifies an `investigate-bug` skill that you must provide separately; this repository alone is not sufficient to start live evaluation. Examples and tasks using environment-specific absolute Python paths are excluded from the distribution.

Before running an evaluation, review the product, model, reasoning effort, CLI version, evaluation and generation limits, time limits, and data sent to external models. The following example assumes that the configuration file is in the repository root. Replace `RUN_ID` and `SHA` with values returned by the commands.

```sh
.venv/bin/agent-evolution doctor --agents codex
.venv/bin/agent-evolution start --config config.json
.venv/bin/agent-evolution baseline --run RUN_ID --split train
.venv/bin/agent-evolution import-candidate --run RUN_ID --body candidate-body.md
.venv/bin/agent-evolution evaluate --run RUN_ID --candidate-sha SHA --split train
.venv/bin/agent-evolution report --run RUN_ID
```

`start` saves the plan and inputs without calling a model. `baseline` and `evaluate` call the model during live evaluation. A candidate file must contain only the skill body, without frontmatter.

The example limits evaluation to four tasks for comparing the current and candidate versions on train. This is insufficient for final adoption, which also requires validation, holdout, and repeated comparisons. Review the additional runs and costs before increasing the limit.

## Candidate generation with GEPA

```sh
.venv/bin/agent-evolution evolve --run RUN_ID
```

The configuration requires a `generator` profile and a `budget.max_generations` value greater than zero. Evaluation and generation can use different models. Candidates are generated from train tasks and results, then selected using validation. Holdout is not supplied during the search. Previous verification records for this snapshot do not confirm live model connections for GEPA candidate generation.

## Final comparison and application

```sh
.venv/bin/agent-evolution select --run RUN_ID --candidate-sha SHA
.venv/bin/agent-evolution finalize --run RUN_ID
.venv/bin/agent-evolution report --run RUN_ID
```

`finalize` freezes the candidate and compares it with the current version on holdout. The defaults require at least five task groups and three repetitions. The decision considers success rates, group-level bootstrap intervals, mandatory checks, and increased execution time. Further search after the candidate is frozen, and reuse of the same holdout in another run, are rejected.

Run the following commands only when the results meet the adoption criteria and a human has reviewed the diff, destination, and report and explicitly requested adoption:

```sh
.venv/bin/agent-evolution approve --run RUN_ID --candidate-sha SHA --report-sha REPORT_SHA
.venv/bin/agent-evolution promote --run RUN_ID --candidate-sha SHA --report-sha REPORT_SHA
.venv/bin/agent-evolution rollback --promotion PROMOTION_ID
```

Approval is bound to the candidate hash, report hash, and destination. Application is rejected if the canonical skill has changed since evaluation. The previous body is saved in SQLite before replacement, and rollback also checks for conflicts with subsequent edits.

## State, budgets, and cancellation

The default state directory is `.evolution/` in the working directory. Place the global `--state PATH` option before the subcommand. State includes SQLite data, compared skills, operation records, checked artifacts, reports, and diffs.

```sh
.venv/bin/agent-evolution --state .evolution cancel --run RUN_ID
```

Budgets limit evaluation and generation starts and elapsed time. They do not impose a strict limit on API round trips within a CLI or on billed costs. Failed starts also consume the budget. Costs that cannot be obtained are shown as `unknown`. Identical completed evaluations are reused within the same run. A run cannot be resumed after cancellation or implementation changes.

## Agent skill and completion observer

The agent skill provides instructions for invoking this CLI from an agent. The optional completion observer proposes starting improvement work when a task ends with unresolved failures. The observer script itself does not start live model evaluation or candidate generation. Approval to start improvement work is separate from approval to apply a skill revision.

The original registration script and skill wrapper assume that `agent-skills/agent-evolution/` is next to the CLI project directory. The bundled skill is located at `skills/agent-evolution/`. This distribution does not support automatic registration from that bundled location. The CLI setup alone does not register skills or configure the observer.

The observer installer changes the user's hook and extension settings. Its hook-identification issue must be considered when reviewing the proposed changes and deciding separately how to install it.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for the full text. Dependencies are covered by their respective licenses.
