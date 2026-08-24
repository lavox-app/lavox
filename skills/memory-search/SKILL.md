---
name: memory-search
description: Search the user's spoken memory (meetings, dictation) and answer with the verbatim quote
---

Search the `lavox-memory` MCP server for: $ARGUMENTS

Use the `search` tool first. When a result carries an anchored assertion (a
decision, commitment or fact), present the structured card AND the verbatim
quote from the transcript chunk. If more context is needed, use `fetch` with
the chunk id to read what was said before and after.
