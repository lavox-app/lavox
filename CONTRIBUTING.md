# Contributing

Lavox is early and moving fast. The most useful contributions right now:

- **Bug reports with recordings' metadata** (never upload audio of other people)
- **Retrieval quality**: eval questions + expected answers from your own usage
- **Windows/Linux capture backends** for the app
- **Docs**: anything that confused you is a bug in the docs

## Ground rules

- The verbatim layer is canonical — no PR that deletes user data or rewrites
  history in the memory store will be accepted. Corrections supersede, never erase.
- Every memory write must carry a `source`. No exceptions.
- Performance features stay free. We don't paywall speed.

Run the server tests with `server/.venv/bin/python3 -m pytest` (where present),
and `cargo test` in `app/src-tauri` for the Rust side.
