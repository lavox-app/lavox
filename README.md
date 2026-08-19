<p align="center">
  <img src="docs/assets/lavox-mark.svg" width="88" alt="Lavox" />
</p>

<h1 align="center">Lavox</h1>

<p align="center">
  A local-first meeting recorder and dictation tool for macOS that turns everything
  you say into a memory any AI can read. Nothing leaves your machine by default.
</p>

<p align="center">
  <a href="https://lavox.app">lavox.app</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-the-memory-works">Architecture</a> ·
  <a href="LICENSE">MIT license</a>
</p>

---

Ask your AI assistant:

> *"What did we decide about the pricing last month — and why not the other option?"*

It answers from your own recorded meetings and voice notes, with the verbatim
quote to prove it. That is what Lavox does.

## The problem

Two problems, one root cause.

**Everything you say evaporates.** Meetings, decisions, promises — spoken, then
gone. Three months later nobody remembers *why* option A won over option B.

**Your AI tools have amnesia.** Every session starts from zero. You re-explain
your projects, your clients, your constraints — every day, to every tool.

Meeting notetakers solve neither: they upload your conversations to their
cloud, summarize, and stop there. Lavox closes the whole loop instead — record
locally, transcribe locally, build a structured memory, and serve that memory
to every AI tool you use through the
[Model Context Protocol](https://modelcontextprotocol.io).

## What it does

| Capability | How |
|---|---|
| Meeting recording | Microphone and system audio, captured on-device. No bot joins your calls — works with Zoom, Meet, Teams, or a conversation at your desk. |
| Speaker identification | Layered diarization: voice profiles, two-track separation, and transcript evidence decide who said what. |
| Dictation anywhere | Hold a hotkey, speak, release — text lands at your cursor. Whisper runs locally via Metal, with silence-hallucination filtering. |
| Decision extraction | An LLM turns speech into typed records: decisions with the **chosen option, the rejected alternatives, and the stated reasoning** — plus facts, commitments, and tasks. |
| A memory with history | Corrections supersede, they never delete. "What did we believe in July?" remains answerable after you change your mind in August. |
| An interface for AI, not for you | No search UI to learn. An MCP server exposes `search`, `fetch`, `timeline`, `remember`, `correct`, and `profile` to Claude Code, Claude Desktop, or any MCP client. |

## How the memory works

```
   meetings · dictation
          │
          ▼  whisper + diarization (on-device)
 ┌─────────────────────────────────────────────┐
 │  VERBATIM LAYER  — the canonical record     │
 │  ~400-token chunks with context headers:    │
 │  [date | kind | title | speaker] + text     │
 ├─────────────────────────────────────────────┤
 │  ASSERTION LAYER — the extracted index      │
 │  decision {chosen, alternatives, reasoning} │
 │  fact · commitment · preference · task      │
 │  bitemporal: occurred_at · invalidated_at · │
 │  superseded_by — corrections never delete   │
 └─────────────────────────────────────────────┘
          │
          ▼  four parallel searches: vector + full-text,
             on both layers → Reciprocal Rank Fusion (k=60)
             → per-recording cap → cross-linking
          │
          ▼  MCP server (stdio)
   Claude Code · Claude Desktop · local models
```

The design follows measured results rather than fashion:

- **The raw transcript is canonical; extraction is an index.** Controlled
  comparisons show verbatim chunks outperform lossy fact-extraction on recall,
  so Lavox keeps both layers and fuses them. Every assertion links back to the
  transcript chunk it came from.
- **Context headers beat clever chunking.** Choice of chunking algorithm moves
  recall by roughly two points; prepending context to each chunk cuts retrieval
  failures by 35–49% in
  [Anthropic's measurements](https://www.anthropic.com/engineering/contextual-retrieval).
  Recording metadata provides that context at zero cost.
- **Recency is an additive, conditional bonus — never a multiplier.**
  Multiplicative age-decay collapsed recall from 19/20 to 4/20 in our tests, so
  the recency term is bounded, additive, and applied only to time-sensitive
  queries.
- **Bitemporality is a schema, not a framework.** Three columns —
  `occurred_at`, `invalidated_at`, `superseded_by` — answer "what did we
  believe then" without a graph database or an LLM call on every write.
- **Writes are disciplined.** Every memory carries a mandatory `source`
  (`extracted` with a transcript anchor, `user_stated`, or `agent`), and
  corrections are soft. This is the defense against memory poisoning: nothing
  enters anonymously, nothing disappears silently.

## Quick start

Requirements: macOS 13+ on Apple Silicon, Rust and pnpm for the app,
Python 3.12 for the server.

```bash
git clone https://github.com/lavox-app/lavox.git && cd lavox

# 1 · The server — transcription, memory, MCP
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8040   # keep running

# 2 · The app — recorder and dictation bar
cd ../app
pnpm install
./build-and-install.command      # builds, signs (ad-hoc by default), installs

# 3 · Wire the memory into Claude Code
claude mcp add lavox-memory -- \
  "$PWD/../server/.venv/bin/python3" "$PWD/../server/mcp_memory.py"
```

Record a meeting or dictate something, then ask Claude to search
`lavox-memory` for it.

Decision extraction is optional. It uses OpenRouter by default, and
`LAVOX_LLM_URL` points it at any OpenAI-compatible endpoint instead:

```bash
# run from the repo root
LAVOX_LLM_KEY=sk-or-... server/.venv/bin/python3 server/extract.py

# or bring your own endpoint (Ollama, vLLM, OpenAI, ...)
LAVOX_LLM_URL=http://localhost:11434/v1/chat/completions \
LAVOX_LLM_KEY=... server/.venv/bin/python3 server/extract.py
```

Without a key, the verbatim layer still does everything else — search,
timeline, full MCP access. The LLM layer is additive, never required.

## Privacy, plainly

- Audio never leaves your machine. Transcription is local
  (faster-whisper / whisper.cpp), embeddings are local (ONNX), and the memory
  is a SQLite file in `~/Lavox/memory/`.
- The only network calls are ones you configure yourself — an optional LLM key
  for extraction, optional Google Calendar, optional self-hosted sync. All of
  them are off by default.
- No telemetry.
- Recording other people is regulated in most jurisdictions. Lavox records
  only when you explicitly start it; obtaining consent is your responsibility.

## Status

Working today: meeting recording with diarization, on-device dictation with
anti-hallucination filtering, the full memory loop (ingest → extraction →
fusion search → MCP), and share links through an optional self-hosted server.

Planned: notarized binary releases, a hosted sync option, a ChatGPT connector
(remote MCP), Windows support.

## License

[MIT](LICENSE). Downloaded models (Whisper, embeddings) carry their own
licenses.
