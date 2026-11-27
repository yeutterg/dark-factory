---
description: Warp's orchestrating agent. Dark Factory's fixed workflow replaces its scheduling.
agentType: FOREMAN
---
# Foreman

Warp uses this agent to schedule the other agents. Dark Factory reads and pins it
but does not run it: its own graph (plan, critique, approve, implement, check,
review, accept) decides what runs and in what order.
