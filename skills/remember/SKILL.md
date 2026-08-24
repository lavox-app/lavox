---
name: remember
description: Save an explicit fact or decision into the user's memory
---

Save this into the `lavox-memory` MCP server using the `remember` tool:
$ARGUMENTS

Set the source to reflect that the user stated it explicitly. If the server
warns about a similar existing memory, show the user the conflict and ask
whether this supersedes it — if yes, use `correct` instead of writing a
duplicate.
