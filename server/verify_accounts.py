#!/usr/bin/env python3
"""
Account-layer verification — the Phase 1 acceptance test.

Usage (against any Postgres that is NOT production data):

    LAVOX_MULTI_TENANT=1 \
    LAVOX_PG_DSN='postgresql://user:pass@host:5432/dbname' \
    .venv/bin/python verify_accounts.py

What it checks:
  1. schema can be created (idempotent)
  2. registration → user + own workspace + token
  3. the password is NEVER in the DB in plaintext (scrypt hash)
  4. login with correct/wrong password
  5. token → user resolution
  6. IDOR: user "A" is NOT a member of user "B"'s workspace
  7. duplicate e-mail rejected
  8. short password rejected
  9. token revocation

The test works with its own random e-mail addresses and cleans up after
itself at the end.
"""

import os
import sys
import uuid

os.environ.setdefault("LAVOX_MULTI_TENANT", "1")

import accounts  # noqa: E402

FAIL = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global FAIL
    mark = "PASS" if ok else "FAIL"
    if not ok:
        FAIL += 1
    print(f"[{mark}] {label}{(' — ' + extra) if extra else ''}")


def main() -> int:
    if not accounts.available():
        print("ERROR: LAVOX_MULTI_TENANT=1 and LAVOX_PG_DSN are required for the test.")
        return 2

    accounts.init_schema()
    accounts.init_schema()  # idempotence
    check("schema can be created and is idempotent", True)

    suffix = uuid.uuid4().hex[:8]
    email_a = f"verify-a-{suffix}@example.com"
    email_b = f"verify-b-{suffix}@example.com"
    pw_a = "Proba-Jelszo-123"
    pw_b = "Masik-Jelszo-456"

    created: list[str] = []
    try:
        a = accounts.register(email_a, pw_a, "Verify A")
        b = accounts.register(email_b, pw_b, "Verify B")
        created += [a["user"]["id"], b["user"]["id"]]

        check("registration → user + workspace + token",
              bool(a["user"]["id"] and a["workspaces"] and a["token"]))
        check("the user owns their own workspace",
              a["workspaces"][0]["role"] == "owner")

        # 3 — the password is not stored recoverably
        with accounts._conn() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE email=%s", (email_a,)
            ).fetchone()
        stored = row["password_hash"]
        check("the password is not in the DB in plaintext",
              pw_a not in stored and stored.startswith("scrypt$"),
              f"stored format: {stored.split('$')[0]}")

        # 4 — login
        check("logs in with the correct password", accounts.login(email_a, pw_a) is not None)
        check("does NOT log in with a wrong password", accounts.login(email_a, "rossz-jelszo") is None)
        check("does NOT log in with an unknown e-mail",
              accounts.login(f"nincs-{suffix}@example.com", pw_a) is None)

        # 5 — token resolution
        principal = accounts.user_by_token(a["token"])
        check("token resolves the user",
              principal is not None and principal["user"]["email"] == email_a)
        check("a fake token resolves nothing",
              accounts.user_by_token("nem-letezo-token") is None)

        # 6 — IDOR: the crux
        ws_a = a["workspaces"][0]["id"]
        ws_b = b["workspaces"][0]["id"]
        check("A is a member of their OWN workspace",
              accounts.is_member(a["user"]["id"], ws_a))
        check("IDOR: A is NOT a member of B's workspace",
              not accounts.is_member(a["user"]["id"], ws_b),
              f"{ws_a} vs {ws_b}")
        check("invalid workspace identifier rejected",
              not accounts.is_member(a["user"]["id"], "../../etc/passwd"))

        # 7-8 — input validation
        try:
            accounts.register(email_a, "Masik-Jelszo-789", "Duplikalt")
            check("duplicate e-mail rejected", False, "did not raise an error")
        except ValueError:
            check("duplicate e-mail rejected", True)

        try:
            accounts.register(f"rovid-{suffix}@example.com", "rovid", "Rovid")
            check("short password rejected", False, "did not raise an error")
        except ValueError:
            check("short password rejected", True)

        # 9 — revocation
        accounts.revoke_token(a["token"])
        check("a revoked token no longer resolves",
              accounts.user_by_token(a["token"]) is None)

    finally:
        # cleanup — workspace/membership/token go via CASCADE
        if created:
            with accounts._conn() as conn:
                conn.execute(
                    "DELETE FROM users WHERE id = ANY(%s)", (created,)
                )
            print(f"\n(cleanup: {len(created)} test users deleted)")

    print()
    if FAIL:
        print(f"RESULT: {FAIL} tests FAILED")
        return 1
    print("RESULT: all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
