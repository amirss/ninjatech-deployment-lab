# NinjaTech Deployment Lab

A production-oriented execution substrate for approved, long-running enterprise work.

The current release demonstrates durable state, idempotency, concurrency control, worker
ownership, crash recovery, bounded retries, cancellation, and observable delivery. It is an
independent portfolio project, not an official NinjaTech product.

## Current evidence

| Area | Status |
| --- | --- |
| FastAPI/PostgreSQL service foundation | Implemented and CI-verified |
| Persistent idempotent task lifecycle | Implemented in PR #1 |
| Leased and fenced worker execution | Implemented in PR #2 |
| End-to-end failure/recovery proof | Implemented in the container smoke workflow |
| Jira/GitHub/Slack workflow | Proposed design; not implemented on `main` |
| LLM reasoning and evaluations | Not implemented |
| Authentication, tenancy, and cloud operation | Not implemented |

The project builds reliability before a reasoning model is granted tool authority. A worker
is not successful because its process says it succeeded; success must be supported by
durable state and evidence that survives retries and ownership changes.

## Start with evidence

- [Source repository](https://github.com/amirss/ninjatech-deployment-lab)
- [CI runs](https://github.com/amirss/ninjatech-deployment-lab/actions)
- [Seven-minute evidence walkthrough](https://github.com/amirss/ninjatech-deployment-lab/blob/main/docs/DEMO.md)
- [Engineering decisions](https://github.com/amirss/ninjatech-deployment-lab/blob/main/docs/DECISIONS.md)

## Explore the system

- [Architecture Overview](Architecture-Overview)
- [Reliable Worker Execution](Reliable-Worker-Execution)
- [Failure Semantics](Failure-Semantics)
- [Enterprise Integration Design — Proposed](Enterprise-Integration-Design)
- [Customer Delivery Walkthrough](FDE-Walkthrough)
- [Evidence-Gated Roadmap](Roadmap)

## Author

Built and maintained by [Amir Seyedi](https://www.linkedin.com/in/amirseyedi), a technical
founder working across agentic AI, enterprise workflows, and regulated decision systems.
