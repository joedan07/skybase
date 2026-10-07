#!/usr/bin/env python3
"""
Build the database from nothing: schema, seed, accounts, demo traffic.

    python init_db.py              # against $DATABASE_URL (or local skybase)
    python init_db.py --no-demo    # schema + seed + accounts only
    python init_db.py --light      # a fifth of the demo traffic

--light exists for remote databases. Every booking goes through book_seats(),
which opens its own connection, and a connection to a managed Postgres in
another region costs about half a second of TLS handshake. The full set is
~690 bookings, which is ten minutes over the wire and twenty seconds locally.

Demo bookings go through db.book_seats(), the same function the website calls,
so the sample data is guaranteed consistent with every constraint and trigger
rather than hand-written to look plausible.
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    # override=False on purpose: a real environment variable always wins over
    # a dotenv file. On Render the config comes from the dashboard, and a
    # stray .env must never be able to silently override it.
    load_dotenv(".env.local", override=False)
    load_dotenv(".env", override=False)
except ImportError:
    pass

import psycopg
from werkzeug.security import generate_password_hash

import db

HERE = Path(__file__).parent

# Demo accounts.
#
# The customer logins are deliberately weak and public — they exist so anyone
# can click around the deployed site.
#
# The ADMIN password is different: this repository is public, so a hard-coded
# admin password would hand the live operations dashboard to anyone who read
# the source. It comes from the environment, and main() refuses to seed a
# remote database with the local default.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "ops@skybase.in")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "skybase-ops")
ADMIN = (ADMIN_EMAIL, ADMIN_PASSWORD, "Operations Control")
CUSTOMERS = [
    ("asha@example.com", "Asha Rao"),
    ("vikram@example.com", "Vikram Shetty"),
    ("neha@example.com", "Neha Pillai"),
    ("imran@example.com", "Imran Qureshi"),
    ("divya@example.com", "Divya Menon"),
    ("rahul@example.com", "Rahul Bose"),
]
CUSTOMER_PASSWORD = "flyskybase"


def run_sql_file(name: str) -> None:
    sql = (HERE / "sql" / name).read_text()
    with psycopg.connect(db.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print(f"  ✓ {name}")


def make_accounts() -> list[int]:
    ids = []
    with psycopg.connect(db.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            email, pw, name = ADMIN
            cur.execute(
                """
                INSERT INTO passenger (email, password_hash, full_name, role)
                VALUES (%s, %s, %s, 'ADMIN')
                ON CONFLICT (email) DO UPDATE
                   SET password_hash = EXCLUDED.password_hash, role = 'ADMIN'
                """,
                (email, generate_password_hash(pw), name),
            )
            for email, name in CUSTOMERS:
                cur.execute(
                    """
                    INSERT INTO passenger (email, password_hash, full_name, phone)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
                    RETURNING passenger_id
                    """,
                    (email, generate_password_hash(CUSTOMER_PASSWORD), name,
                     f"9{random.randint(100000000, 999999999)}"),
                )
                ids.append(cur.fetchone()[0])
    print(f"  ✓ 1 admin + {len(ids)} customers")
    return ids


def make_demo_traffic(customer_ids: list[int], light: bool = False) -> None:
    """Sell a realistic scatter of seats so the reports have something to say."""
    random.seed(18)
    back, fwd = (1, 2) if light else (3, 4)
    flights = db.query(
        """
        SELECT f.flight_id, f.dep_date, f.base_fare,
               (SELECT count(*) FROM seat WHERE aircraft_id = f.aircraft_id) AS cap
          FROM flight f
         WHERE f.dep_date BETWEEN CURRENT_DATE - %s AND CURRENT_DATE + %s
         ORDER BY f.sched_dep
        """,
        (back, fwd),
    )

    booked = cancelled = 0
    for f in flights:
        # Past flights flew fuller than flights still on sale.
        past = f["dep_date"] < __import__("datetime").date.today()
        target = int(f["cap"] * (random.uniform(0.45, 0.82) if past
                                 else random.uniform(0.08, 0.38)))
        if light:
            target = max(2, target // 3)

        free = db.query(
            """
            SELECT s.seat_id, s.cabin FROM seat s
              JOIN flight fl ON fl.aircraft_id = s.aircraft_id
             WHERE fl.flight_id = %s
               AND NOT EXISTS (SELECT 1 FROM ticket t
                                WHERE t.seat_id = s.seat_id
                                  AND t.flight_id = fl.flight_id
                                  AND t.status <> 'CANCELLED')
             ORDER BY random()
            """,
            (f["flight_id"],),
        )
        pool = free[:target]

        while pool:
            group = pool[: random.choice([1, 1, 1, 2, 2, 3])]
            pool = pool[len(group):]
            cabin = group[0]["cabin"]
            group = [s for s in group if s["cabin"] == cabin] or group[:1]
            try:
                res = db.book_seats(
                    random.choice(customer_ids),
                    f["flight_id"],
                    [s["seat_id"] for s in group],
                    cabin,
                    random.choice(["CARD", "UPI", "NETBANKING"]),
                )
                booked += 1
                # About one booking in fourteen gets cancelled, so the refund
                # path and the negative payment rows are exercised too.
                if random.random() < 0.07:
                    db.cancel_booking(res["booking_id"])
                    cancelled += 1
            except Exception as e:  # noqa: BLE001
                print(f"    · skipped a group on flight {f['flight_id']}: "
                      f"{type(e).__name__}")

    print(f"  ✓ {booked} bookings ({cancelled} later cancelled)")

    # Now that the history exists, bring past flights up to date. The trigger
    # trg_flight_sellable refuses to sell on a flown flight, which is exactly
    # why this runs last rather than in seed.sql.
    n = db.execute(
        """
        UPDATE flight SET status = 'ARRIVED'
         WHERE dep_date < CURRENT_DATE AND status = 'SCHEDULED'
        """
    )
    print(f"  ✓ {n} past flights marked ARRIVED")

    # Check a few of today's passengers in, so the manifest is not all blanks.
    done = db.execute(
        """
        UPDATE ticket SET status = 'CHECKED_IN', checked_in_at = CURRENT_TIMESTAMP
         WHERE ticket_id IN (
            SELECT t.ticket_id FROM ticket t JOIN flight f USING (flight_id)
             WHERE f.dep_date = CURRENT_DATE AND t.status = 'CONFIRMED'
             ORDER BY random() LIMIT 12)
        """
    )
    print(f"  ✓ {done} passengers checked in")


def is_local(url: str) -> bool:
    return ("@" not in url) or ("localhost" in url) or ("127.0.0.1" in url)


def main() -> None:
    demo = "--no-demo" not in sys.argv
    light = "--light" in sys.argv
    target = db.DATABASE_URL
    shown = target.split("@")[-1] if "@" in target else target

    # Refuse to put the local default admin password on a remote database.
    if not is_local(target) and ADMIN_PASSWORD == "skybase-ops":
        sys.exit(
            "\nRefusing to seed a remote database with the default admin "
            "password.\n"
            "This repository is public, so set your own first:\n\n"
            "    export ADMIN_PASSWORD='<something long>'\n"
            "    python init_db.py\n\n"
            "On Render, add ADMIN_PASSWORD as an environment variable.\n"
        )

    print(f"\nSkyBase · building database at {shown}\n")

    print("schema and reference data")
    run_sql_file("schema.sql")
    run_sql_file("seed.sql")

    print("accounts")
    ids = make_accounts()

    if demo:
        print("demo traffic" + (" (light)" if light else ""))
        make_demo_traffic(ids, light=light)

    counts = db.query(
        """
        SELECT (SELECT count(*) FROM airport)   AS airports,
               (SELECT count(*) FROM route)     AS routes,
               (SELECT count(*) FROM aircraft)  AS aircraft,
               (SELECT count(*) FROM seat)      AS seats,
               (SELECT count(*) FROM flight)    AS flights,
               (SELECT count(*) FROM passenger) AS passengers,
               (SELECT count(*) FROM booking)   AS bookings,
               (SELECT count(*) FROM ticket)    AS tickets,
               (SELECT count(*) FROM payment)   AS payments
        """,
        one=True,
    )
    print("\nrow counts")
    for k, v in counts.items():
        print(f"  {k:<11} {v:>5}")

    shown_pw = ADMIN[1] if is_local(target) else "(the ADMIN_PASSWORD you set)"
    print(f"""
sign in
  admin      {ADMIN[0]}  /  {shown_pw}
  customer   {CUSTOMERS[0][0]}  /  {CUSTOMER_PASSWORD}

  python app.py      then open http://127.0.0.1:5001
""")


if __name__ == "__main__":
    main()
