"""
Database access for SkyBase.

Every statement in this project is hand-written SQL, on purpose: this is a DBMS
assignment, and an ORM would hide exactly the thing being assessed.  The
interesting function here is book_seats() — the booking transaction that makes
the seat guarantee true under concurrency.
"""

from __future__ import annotations

import atexit
import os
import random
import secrets
import time
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# ─────────────────────────────────────────────────────────────── connection

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///skybase")

# Neon hands out URLs with the postgres:// prefix and needs TLS.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if "neon.tech" in DATABASE_URL and "sslmode=" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"


# prepare_threshold=None disables psycopg's automatic prepared statements.
# Neon's pooled endpoint is PgBouncer in transaction mode, where a statement
# prepared on one backend connection may not exist on the next one, which
# surfaces as a baffling "prepared statement does not exist". Nothing here is
# hot enough to miss them.
_CONN_KW = {"row_factory": dict_row, "prepare_threshold": None}

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """A connection pool, created on first use.

    Opening a connection to a managed Postgres costs a TLS handshake, and the
    home page alone runs three queries: without a pool every page view paid for
    three handshakes. Built lazily rather than at import so that gunicorn
    workers each get their own after forking, and so importing this module
    never requires a reachable database.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX", 6)),
            max_idle=120,
            timeout=15,
            kwargs=_CONN_KW,
            open=True,
        )
        # A pool keeps worker threads alive. Without this, any short script
        # that touches the database (init_db.py, a one-off query) prints
        # "couldn't stop thread pool-1-worker-0" on its way out.
        atexit.register(close_pool)
    return _pool


def close_pool() -> None:
    """Shut the pool down. Idempotent, so atexit may call it after a caller has."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connect(autocommit: bool = True):
    """Borrow a pooled connection.

    Autocommit is ON by default, so a single-statement helper cannot silently
    lose an INSERT ... RETURNING by returning the connection without
    committing. The booking, cancellation and check-in paths pass
    autocommit=False, because each is several statements that must land
    together or not at all.
    """
    with pool().connection() as conn:
        # Safe to flip here: the pool only hands back connections with no
        # transaction in flight.
        if conn.autocommit != autocommit:
            conn.autocommit = autocommit
        yield conn


def query(sql: str, params: tuple | None = None, one: bool = False):
    # params stays None when there are none: psycopg only scans for %s
    # placeholders if a sequence is passed, and a bare LIKE '%WHERE%' in a
    # catalogue query would otherwise be mistaken for one.
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql: str, params: tuple | None = None) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        n = cur.rowcount
    return n


# ───────────────────────────────────────────────────────────────── helpers

# No I, O, 0 or 1 — a PNR gets read aloud over a phone.
_PNR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

BUSINESS_MULTIPLIER = 2.4
CANCEL_FEE_EARLY = 0.10   # >= 24h before departure
CANCEL_FEE_LATE = 0.50    # <  24h before departure


def new_pnr() -> str:
    return "".join(secrets.choice(_PNR_ALPHABET) for _ in range(6))


def new_ticket_no() -> str:
    return "SB-" + "".join(str(random.randint(0, 9)) for _ in range(10))


def new_txn_ref(prefix: str = "TXN") -> str:
    return f"{prefix}{int(time.time() * 1000) % 10**10}{random.randint(100, 999)}"


def fare_for(base_fare, cabin: str):
    base = float(base_fare)
    return round(base * BUSINESS_MULTIPLIER, 2) if cabin == "BUSINESS" else round(base, 2)


class SeatUnavailable(Exception):
    """Raised when a seat was taken between the customer seeing it and paying."""

    def __init__(self, seat_nos):
        self.seat_nos = seat_nos
        super().__init__(f"seat(s) no longer available: {', '.join(seat_nos)}")


# ──────────────────────────────────────────────────── the booking transaction


def book_seats(
    passenger_id: int,
    flight_id: int,
    seat_ids: list[int],
    cabin: str,
    method: str = "CARD",
    *,
    use_row_lock: bool = True,
    hold_ms: int = 0,
):
    """Book one or more seats on a flight, atomically.

    Either a booking, its tickets and its payment all exist, or none of them do.

    Three independent defences keep two passengers out of one seat:

      1. SELECT ... FOR UPDATE on the seat rows (below).  The first transaction
         takes an exclusive row lock; a second transaction *blocks* on that
         statement instead of reading a stale "seat is free".

      2. The partial unique index uq_ticket_seat_per_flight.  The declarative
         backstop — it holds even if a future code path forgets the lock, runs
         at a weaker isolation level, or connects through a different client.

      3. The capacity trigger, which refuses the (N+1)th ticket.

    `use_row_lock` and `hold_ms` exist so the admin Concurrency Lab can turn
    defence 1 off and watch defence 2 do the work.  Production always locks.
    """
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            # ── 1 · pessimistic lock ─────────────────────────────────────
            # ORDER BY seat_id so two multi-seat bookings always take their
            # locks in the same order: without it, A locking 12A then 12B
            # while B locks 12B then 12A would deadlock.
            if use_row_lock:
                cur.execute(
                    """
                    SELECT seat_id FROM seat
                     WHERE seat_id = ANY(%s)
                     ORDER BY seat_id
                       FOR UPDATE
                    """,
                    (seat_ids,),
                )

            # The cabin is a property of the seat, so read it from the seat
            # rather than believing the form: otherwise a crafted POST could
            # buy a business seat at the economy fare.
            cur.execute(
                "SELECT seat_id, seat_no, cabin FROM seat WHERE seat_id = ANY(%s)",
                (seat_ids,),
            )
            seat_rows = cur.fetchall()
            if len(seat_rows) != len(set(seat_ids)):
                conn.rollback()
                raise ValueError("unknown seat")
            cabins = {r["cabin"] for r in seat_rows}
            if len(cabins) > 1:
                conn.rollback()
                raise ValueError("one booking cannot mix cabins")
            cabin = cabins.pop()

            # Simulates a slow payment step while the lock is held, so the
            # race is observable in the lab.
            if hold_ms:
                time.sleep(hold_ms / 1000.0)

            # ── 2 · is the seat still free? ──────────────────────────────
            cur.execute(
                """
                SELECT s.seat_no
                  FROM ticket t
                  JOIN seat   s ON s.seat_id = t.seat_id
                 WHERE t.flight_id = %s
                   AND t.seat_id = ANY(%s)
                   AND t.status <> 'CANCELLED'
                """,
                (flight_id, seat_ids),
            )
            taken = [r["seat_no"] for r in cur.fetchall()]
            if taken:
                conn.rollback()
                raise SeatUnavailable(taken)

            # ── 3 · the booking ─────────────────────────────────────────
            booking_id = None
            for _ in range(5):  # retry on the astronomically unlikely PNR clash
                try:
                    cur.execute(
                        "INSERT INTO booking (pnr, passenger_id) VALUES (%s, %s)"
                        " RETURNING booking_id, pnr",
                        (new_pnr(), passenger_id),
                    )
                    row = cur.fetchone()
                    booking_id, pnr = row["booking_id"], row["pnr"]
                    break
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
            if booking_id is None:
                raise RuntimeError("could not allocate a PNR")

            # ── 4 · the tickets ─────────────────────────────────────────
            cur.execute("SELECT base_fare FROM flight WHERE flight_id = %s", (flight_id,))
            base_fare = cur.fetchone()["base_fare"]

            total = 0.0
            for seat_id in seat_ids:
                amount = fare_for(base_fare, cabin)
                total += amount
                cur.execute(
                    """
                    INSERT INTO ticket (ticket_no, booking_id, flight_id,
                                        passenger_id, seat_id, cabin, fare_paid)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (new_ticket_no(), booking_id, flight_id,
                     passenger_id, seat_id, cabin, amount),
                )

            # ── 5 · the money ───────────────────────────────────────────
            cur.execute(
                "INSERT INTO payment (booking_id, txn_ref, amount, method)"
                " VALUES (%s, %s, %s, %s)",
                (booking_id, new_txn_ref(), total, method),
            )

            conn.commit()
            return {"booking_id": booking_id, "pnr": pnr, "total": total}


def cancel_booking(booking_id: int, passenger_id: int | None = None):
    """Cancel every live ticket on a booking, release the seats, and write the
    refund as a negative payment row.  One transaction.

    The refund comes off the fare *frozen on the ticket*, not the flight's
    current base_fare — which is the whole reason fare_paid exists.
    """
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.booking_id, b.status, b.passenger_id
                  FROM booking b WHERE b.booking_id = %s FOR UPDATE
                """,
                (booking_id,),
            )
            booking = cur.fetchone()
            if not booking:
                raise ValueError("no such booking")
            if passenger_id is not None and booking["passenger_id"] != passenger_id:
                raise PermissionError("not your booking")
            if booking["status"] == "CANCELLED":
                conn.rollback()
                return {"refund": 0.0, "already": True}

            cur.execute(
                """
                SELECT t.ticket_id, t.fare_paid, f.sched_dep,
                       f.sched_dep - CURRENT_TIMESTAMP AS lead_time,
                       f.status AS flight_status
                  FROM ticket t JOIN flight f ON f.flight_id = t.flight_id
                 WHERE t.booking_id = %s AND t.status <> 'CANCELLED'
                """,
                (booking_id,),
            )
            tickets = cur.fetchall()

            refund = 0.0
            for t in tickets:
                hours = t["lead_time"].total_seconds() / 3600
                if t["flight_status"] in ("DEPARTED", "ARRIVED") or hours <= 0:
                    fee = 1.0           # flown: nothing comes back
                elif hours >= 24:
                    fee = CANCEL_FEE_EARLY
                else:
                    fee = CANCEL_FEE_LATE
                refund += round(float(t["fare_paid"]) * (1 - fee), 2)

                # Releasing the seat (seat_id -> NULL) is what lets the partial
                # unique index free 14A for the next passenger.
                cur.execute(
                    "UPDATE ticket SET status = 'CANCELLED', seat_id = NULL,"
                    " checked_in_at = NULL WHERE ticket_id = %s",
                    (t["ticket_id"],),
                )

            cur.execute(
                "UPDATE booking SET status = 'CANCELLED' WHERE booking_id = %s",
                (booking_id,),
            )

            if refund > 0:
                cur.execute(
                    "INSERT INTO payment (booking_id, txn_ref, amount, method)"
                    " VALUES (%s, %s, %s, 'REFUND')",
                    (booking_id, new_txn_ref("RFND"), -refund),
                )

            conn.commit()
            return {"refund": round(refund, 2), "tickets": len(tickets)}


def check_in(ticket_id: int, passenger_id: int | None = None):
    """Web check-in. The CHECK constraint chk_ticket_checkin guarantees status
    and checked_in_at can never disagree, so both move together."""
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.ticket_id, t.status, b.passenger_id, f.status AS fs
                  FROM ticket t
                  JOIN booking b ON b.booking_id = t.booking_id
                  JOIN flight  f ON f.flight_id  = t.flight_id
                 WHERE t.ticket_id = %s FOR UPDATE OF t
                """,
                (ticket_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("no such ticket")
            if passenger_id is not None and row["passenger_id"] != passenger_id:
                raise PermissionError("not your ticket")
            if row["status"] != "CONFIRMED":
                conn.rollback()
                return {"ok": False, "why": f"ticket is {row['status'].lower()}"}
            if row["fs"] in ("DEPARTED", "ARRIVED", "CANCELLED"):
                conn.rollback()
                return {"ok": False, "why": f"flight has {row['fs'].lower()}"}

            cur.execute(
                "UPDATE ticket SET status = 'CHECKED_IN',"
                " checked_in_at = CURRENT_TIMESTAMP WHERE ticket_id = %s",
                (ticket_id,),
            )
            conn.commit()
            return {"ok": True}
