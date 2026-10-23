# 02 — Reliable operations agent

**Build second.** Reuse the working model endpoint from project 1. This is the recommended main product demonstration.

## Objective

Build an agent that investigates incidents in a small application, identifies relevant evidence, proposes a fix, and performs permitted recovery actions. It must preserve progress across interruption and avoid repeating completed actions.

## Why this fits Amazon and Microsoft

[Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html) develops agent runtime, identity, memory, tools, and observability. [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework) includes agent workflows, persistence, checkpoints, and hosting.

2028+ assessment: trustworthy tool execution and failure recovery are durable problems. The project's value comes from correctness and evidence, not the number of agents.

## Minimum viable scope

- A small application with an API, worker, database, and telemetry.
- Approximately 10 controlled incident types, such as worker outage, bad configuration, or queue backlog.
- Tools to read health, logs, metrics, and runbooks.
- A small allowlist of sandbox recovery actions.
- Persistent workflow state, action audit records, and a visible incident report.

## Proposed stack and architecture

Python, FastAPI, PostgreSQL, Docker Compose, OpenTelemetry, and the local model server. Choose Strands or Microsoft Agent Framework after checking current local-model integration. Use explicit state transitions rather than leaving execution entirely to model-generated text.

```text
Incident -> Workflow -> Read tools -> Evidence -> Proposed action
                |                                  |
          Persistent state                  Authorization policy
                                                   |
                                            Sandbox action tool
```

## Implementation stages

1. **Deterministic baseline:** write scripted diagnostics and recovery runbooks. Exit when incidents are reproducible without an LLM.
2. **Read-only agent:** let the model investigate and produce an evidence-linked diagnosis. Exit when you can score diagnoses independently.
3. **Permitted execution:** validate structured tool arguments and authorize each action. Require approval for actions outside the sandbox allowlist.
4. **Durability:** persist state and action IDs. Add idempotency at the tool's effect boundary. Exit when retries after interruption do not repeat the effect.
5. **Evaluation and UI:** display evidence, tool trace, action result, and unresolved uncertainty. Add a repeatable failure demo.

## Data and evaluation

Generate approximately 100 incident runs across different configurations. Separate development incidents from held-out scenarios. Include healthy-system cases where no recovery action is justified.

Compare the scripted runbook, a simple prompted agent, and the durable agent. Report correct diagnoses, completed recoveries, unnecessary actions, model calls, and recovery time. Distinguish the model's claimed success from an independently verified healthy service.

## Important tests

- Interrupt after a successful action but before the workflow records its result; retry must not repeat the effect.
- The tool times out after possibly completing; reconcile the actual service state before another attempt.
- Logs contain prompt-injection text; the agent cannot gain new tool permissions from it.
- Evidence is insufficient; the agent stops or escalates instead of guessing an action.
- A healthy service generates a misleading alert; unnecessary action is counted as a failure.

Retries alone do not guarantee exactly-once effects. Explain which operations are idempotent, how ambiguous completion is resolved, and which guarantees remain limited.

## GX10 and deployment

Keep one model loaded and run the target application as lightweight containers. Docker Compose is enough to begin. A later AWS or Azure deployment can demonstrate cloud identity and monitoring; keep permissions narrow and include teardown instructions.

## Definition of done

The demo breaks a service, diagnoses it, executes an authorized repair, verifies health, and survives an injected interruption. Publish the baseline comparison, failed scenarios, and audit trace.

Deliver application fixtures, fault injection scripts, tool schemas, workflow persistence code, evaluation cases, reports, and a short demo. Résumé template: “Built a durable incident-response agent; achieved [measured recovery result] on [held-out cases], with audited and idempotent sandbox actions.”

## Keep out of the first version

Access to real production systems, unrestricted shell tools, autonomous spending, and complex multi-agent orchestration.
