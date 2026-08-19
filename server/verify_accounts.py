#!/usr/bin/env python3
"""
Fiók-réteg verifikáció — a Fázis 1 elfogadási tesztje.

Futtatás (bármilyen Postgres ellen, ami NEM a produkciós adat):

    LAVOX_MULTI_TENANT=1 \
    LAVOX_PG_DSN='postgresql://user:pass@host:5432/dbname' \
    .venv/bin/python verify_accounts.py

Amit ellenőriz:
  1. séma létrehozható (idempotens)
  2. regisztráció → user + saját workspace + token
  3. a jelszó SOHA nem plaintextben van a DB-ben (scrypt hash)
  4. helyes/rossz jelszavas belépés
  5. token → user feloldás
  6. IDOR: user "A" NEM tagja user "B" workspace-ének
  7. duplikált e-mail elutasítva
  8. rövid jelszó elutasítva
  9. token visszavonás

A teszt saját, véletlen e-mail címekkel dolgozik, és a végén takarít maga után.
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
        print("HIBA: LAVOX_MULTI_TENANT=1 és LAVOX_PG_DSN kell a teszthez.")
        return 2

    accounts.init_schema()
    accounts.init_schema()  # idempotencia
    check("séma létrehozható és idempotens", True)

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

        check("regisztráció → user + workspace + token",
              bool(a["user"]["id"] and a["workspaces"] and a["token"]))
        check("a saját workspace tulajdonosa a user",
              a["workspaces"][0]["role"] == "owner")

        # 3 — a jelszó nem tárolódik visszafejthetően
        with accounts._conn() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE email=%s", (email_a,)
            ).fetchone()
        stored = row["password_hash"]
        check("a jelszó nincs plaintextben a DB-ben",
              pw_a not in stored and stored.startswith("scrypt$"),
              f"tárolt formátum: {stored.split('$')[0]}")

        # 4 — belépés
        check("helyes jelszóval belép", accounts.login(email_a, pw_a) is not None)
        check("rossz jelszóval NEM lép be", accounts.login(email_a, "rossz-jelszo") is None)
        check("ismeretlen e-maillel NEM lép be",
              accounts.login(f"nincs-{suffix}@example.com", pw_a) is None)

        # 5 — token feloldás
        principal = accounts.user_by_token(a["token"])
        check("token feloldja a usert",
              principal is not None and principal["user"]["email"] == email_a)
        check("hamis token nem old fel semmit",
              accounts.user_by_token("nem-letezo-token") is None)

        # 6 — IDOR: a lényeg
        ws_a = a["workspaces"][0]["id"]
        ws_b = b["workspaces"][0]["id"]
        check("A tagja a SAJÁT workspace-ének",
              accounts.is_member(a["user"]["id"], ws_a))
        check("IDOR: A NEM tagja B workspace-ének",
              not accounts.is_member(a["user"]["id"], ws_b),
              f"{ws_a} vs {ws_b}")
        check("érvénytelen workspace-azonosító elutasítva",
              not accounts.is_member(a["user"]["id"], "../../etc/passwd"))

        # 7-8 — input validáció
        try:
            accounts.register(email_a, "Masik-Jelszo-789", "Duplikalt")
            check("duplikált e-mail elutasítva", False, "nem dobott hibát")
        except ValueError:
            check("duplikált e-mail elutasítva", True)

        try:
            accounts.register(f"rovid-{suffix}@example.com", "rovid", "Rovid")
            check("rövid jelszó elutasítva", False, "nem dobott hibát")
        except ValueError:
            check("rövid jelszó elutasítva", True)

        # 9 — visszavonás
        accounts.revoke_token(a["token"])
        check("visszavont token már nem old fel",
              accounts.user_by_token(a["token"]) is None)

    finally:
        # takarítás — a workspace/tagság/token CASCADE-del megy
        if created:
            with accounts._conn() as conn:
                conn.execute(
                    "DELETE FROM users WHERE id = ANY(%s)", (created,)
                )
            print(f"\n(takarítás: {len(created)} teszt-user törölve)")

    print()
    if FAIL:
        print(f"EREDMÉNY: {FAIL} teszt BUKOTT")
        return 1
    print("EREDMÉNY: minden teszt átment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
