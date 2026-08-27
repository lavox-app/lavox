# Contributing

Lavox is early and moving fast. The most useful contributions right now:

- **Bug reports with recording metadata** (never upload audio of other people)
- **Retrieval quality**: eval questions and expected answers drawn from your own
  usage (see `server/eval/`)
- **Windows/Linux capture backends** for the app
- **Docs**: anything that confused you is a bug in the docs

## Ground rules

- The verbatim layer is canonical: no PR that deletes user data or rewrites
  history in the memory store will be accepted. Corrections supersede, never erase.
- Every memory write must carry a `source`. No exceptions.
- Performance features stay free. We don't paywall speed.

## Tests

- Rust: `cargo test` in `app/src-tauri`.
- Server: there is no pytest suite yet. `server/verify_accounts.py` exercises the
  account layer against a scratch Postgres instance, and `server/eval/run_eval.py`
  scores retrieval against your own memory.
