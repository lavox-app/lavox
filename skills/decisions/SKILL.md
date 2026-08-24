---
name: decisions
description: List the user's recent decisions from memory, with chosen option, rejected alternatives and reasoning
---

Query the `lavox-memory` MCP server for recent decisions. Use `timeline` (or
`search` with the topic in $ARGUMENTS if given) and filter for decision-type
assertions. Present each as: what was chosen, what was rejected, the stated
reasoning, and when it was decided. Mark superseded decisions clearly and show
what replaced them.
