# Lavox

**Your voice, remembered.** A local-first meeting recorder and dictation tool for macOS that turns everything you say into a memory **any AI can read** — Claude, ChatGPT, or a local model. Nothing leaves your machine.

> Ask your AI: *"What did we decide about the pricing last month — and why not the other option?"*
> It answers from your own recorded meetings and voice notes, with the verbatim quote to prove it.

---

## Why this exists

Two problems, one root cause:

1. **Everything you say evaporates.** Meetings, decisions, promises — spoken, then gone. Three months later nobody remembers *why* you chose option A over option B.
2. **Your AI tools have amnesia.** Every Claude or ChatGPT session starts from zero. You re-explain your projects, your clients, your decisions — every single day.

Lavox fixes both with one loop: **record locally → transcribe locally → build a memory → serve it to every AI tool you use** via [MCP](https://modelcontextprotocol.io).

Meeting notetakers send your conversations to their cloud. Three of the market leaders are currently facing privacy lawsuits or have suffered breaches for exactly that reason. Lavox takes the opposite bet: **the recording, the transcript, and the memory built from them stay on your Mac.**

## What it does

| | |
|---|---|
| 🎙️ **Record meetings** | Mic + system audio, no bot joining your calls. Works with Zoom, Meet, Teams, anything. |
| 🗣 **Speaker identification** | Multi-layer diarization (voice profiles, two-track separation, transcript evidence) — knows *who* said what. |
| ⌨️ **Dictate anywhere** | Hold a hotkey, speak, release — text lands at your cursor. Whisper runs on-device via Metal. |
| 🧠 **Memory that understands** | An LLM extracts **decision cards** from your speech: what was chosen, **which alternatives were rejected, and why**. Facts, commitments and tasks too. |
| 🕰 **Nothing is ever lost** | Change your mind? The old decision is *superseded*, never deleted. Ask "what did we believe in July?" and get an answer. |
| 🔌 **Any AI can read it** | An MCP server exposes `search` / `fetch` / `timeline` / `remember` / `correct` / `profile`. Wire it into Claude Code, Claude Desktop, or ChatGPT. |

## How the memory works

```
   meetings · dictation
          │
          ▼  whisper + diarization (on-device)
 ┌─────────────────────────────────────────────┐
 │  VERBATIM LAYER  — the canonical record     │
 │  ~400-token chunks with context headers     │
 │  [date | kind | title | speaker] + text     │
 ├─────────────────────────────────────────────┤
 │  ASSERTION LAYER — the extracted index      │
 │  decision {chosen, alternatives, reasoning} │
 │  fact · commitment · preference · task      │
 │  bitemporal: occurred_at / invalidated_at / │
 │  superseded_by — corrections never delete   │
 └─────────────────────────────────────────────┘
          │
          ▼  4 parallel searches (vector + BM25, both layers)
             → Reciprocal Rank Fusion (k=60)
             → per-recording cap → cross-linking
          │
          ▼  MCP server (stdio)
   Claude Code · Claude Desktop · local models
```

Design choices are evidence-based, not vibes-based:

- **The raw transcript is canonical; extraction is an index.** Controlled measurements show verbatim chunks *beat* lossy fact-extraction on recall — so we keep both and fuse them. Every assertion links back to its source chunk.
- **Context headers instead of clever chunking.** Chunk-algorithm choice moves recall by ~2 points; adding context to chunks cuts retrieval failures by 35–49% ([Anthropic's measurements](https://www.anthropic.com/engineering/contextual-retrieval)). Recording metadata gives us that context for free.
- **Recency is an additive, conditional bonus — never a multiplier.** Multiplicative age-decay collapsed recall from 19/20 to 4/20 in our tests.
- **Bitemporality is a schema, not a framework.** Three columns (`occurred_at`, `invalidated_at`, `superseded_by`) give you "what did we believe then" without a graph database or an LLM call per write.
- **Writes are disciplined.** Every memory carries a mandatory `source` (`extracted` with a transcript anchor, `user_stated`, or `agent`). Corrections are soft: the old version stays retrievable. This is the memory-poisoning defense.

## Quick start

**Requirements:** macOS 13+ (Apple Silicon), Rust + pnpm for the app, Python 3.12 for the server.

```bash
git clone https://github.com/lavox-app/lavox.git && cd lavox

# 1. The server (transcription + memory + MCP)
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8040   # keep it running

# 2. The app (recorder + dictation bar)
cd ../app
pnpm install
./build-and-install.command        # builds, signs (ad-hoc by default), installs

# 3. Wire the memory into Claude Code
claude mcp add lavox-memory -- \
  "$PWD/../server/.venv/bin/python3" "$PWD/../server/mcp_memory.py"
```

Record a meeting or dictate something, then ask Claude: *"Search lavox-memory for what I said about the roadmap."*

**Decision extraction** (optional, needs any OpenAI-compatible key):

```bash
LAVOX_LLM_KEY=sk-or-... .venv/bin/python3 server/extract.py
```

Without a key the verbatim layer still works — search, timeline, everything. The LLM layer is additive, never required.

## What's true about privacy

- Audio never leaves your machine. Transcription is local (faster-whisper / whisper.cpp), embeddings are local (ONNX), the memory is a SQLite file in `~/Lavox/memory/`.
- The only network calls are the ones **you** configure: an optional LLM key for extraction, optional cloud sync (off by default), optional Google Calendar.
- No telemetry. None.
- **Recording other people is regulated.** You are responsible for complying with consent laws where you live. Lavox records only when you explicitly start it.

## Status

Working today: meeting recording with diarization, on-device dictation with anti-hallucination filtering, the full memory loop (ingest → extraction → fusion search → MCP), share links via optional self-hosted server.

On the roadmap: notarized binary releases, cloud sync as a hosted option, ChatGPT connector (remote MCP), Windows.

## License

[MIT](LICENSE). The models it downloads (Whisper, embedding models) carry their own licenses.
