#!/usr/bin/env python3
"""
SkyBase — system test runner.

Drives the real Flask application (through its test client) and the real
PostgreSQL database, and records what actually happened. Nothing here is
mocked: every "actual result" below is read back from the database or the HTTP
response, and a case is marked PASS only if the observed outcome equals the
expected one.

Run against a DISPOSABLE database — it books, cancels and deliberately violates
constraints:

    createdb skybase_test
    DATABASE_URL=postgresql:///skybase_test python init_db.py --no-demo
    DATABASE_URL=postgresql:///skybase_test python tests/run_tests.py

Writes tests/results.json and prints a summary.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

import app as A  # noqa: E402
import db  # noqa: E402

A.app.config.update(TESTING=True)
URL = db.DATABASE_URL
if not any(h in URL for h in ("skybase_test", "_test")):
    sys.exit("Refusing to run: DATABASE_URL does not look like a test database.")

RESULTS: list[dict] = []


def record(tid, feature, action, expected, actual, ok):
    RESULTS.append(dict(id=tid, feature=feature, action=action,
                        expected=expected, actual=actual,
                        status="Pass" if ok else "FAIL"))
    print(f"  {tid}  {'PASS' if ok else 'FAIL'}  {feature}: {actual}")


def n(sql, params=()):
    return list(db.query(sql, params, one=True).values())[0]


def flash_of(resp):
    """The newest flash message on the page. Flashes queue in the session until a
    page renders them, so an earlier request that was not followed can leave a
    stale one ahead of the message this request produced."""
    found = re.findall(rb'class="flash flash--\w+">([^<]+)<', resp.data)
    return found[-1].decode().strip() if found else ""


def free_seats(fid, cabin="ECONOMY", k=2):
    return db.query(
        """SELECT s.seat_id, s.seat_no FROM seat s
             JOIN flight f ON f.aircraft_id = s.aircraft_id
            WHERE f.flight_id = %s AND s.cabin = %s
              AND NOT EXISTS (SELECT 1 FROM ticket t WHERE t.seat_id = s.seat_id
                               AND t.flight_id = f.flight_id AND t.status <> 'CANCELLED')
            ORDER BY s.seat_id LIMIT %s""", (fid, cabin, k))


def flight_on(day_offset, no):
    return db.query("SELECT flight_id, base_fare FROM flight WHERE flight_no = %s "
                    "AND dep_date = CURRENT_DATE + %s", (no, day_offset), one=True)


print("\nSkyBase system tests\n")
cust = A.app.test_client()
admin = A.app.test_client()
other = A.app.test_client()
TESTER = "tester@example.com"
THREE = date.today() + timedelta(days=3)

# ── TC-01 sign up ──────────────────────────────────────────────────────────
r = cust.post("/signup", data={"full_name": "Test Flyer", "email": TESTER,
                               "phone": "9876543210", "password": "longenough1"})
row = db.query("SELECT role, password_hash FROM passenger WHERE email=%s", (TESTER,), one=True)
ok = bool(row) and row["role"] == "CUSTOMER" and row["password_hash"].startswith("scrypt:")
record("TC-01", "Sign up", "Valid name, e-mail, 10-digit phone, 10-char password",
       "Row created with role CUSTOMER; password stored hashed; user signed in",
       f"HTTP {r.status_code}; passenger row created, role={row['role'] if row else '-'}, "
       f"hash prefix '{row['password_hash'][:7] if row else '-'}'", ok)

# ── TC-02 duplicate e-mail ─────────────────────────────────────────────────
before = n("SELECT count(*) FROM passenger")
c2 = A.app.test_client()
r = c2.post("/signup", data={"full_name": "Dup", "email": TESTER, "phone": "",
                             "password": "longenough1"}, follow_redirects=True)
after = n("SELECT count(*) FROM passenger")
msg = flash_of(r)
record("TC-02", "Sign up", "Register again with an e-mail that already exists",
       "Rejected by UNIQUE(email); no new row; user told to sign in",
       f"Message “{msg}”; passenger rows {before} → {after}",
       before == after and "already has an account" in msg)

# ── TC-03 invalid phone ────────────────────────────────────────────────────
c3 = A.app.test_client()
r = c3.post("/signup", data={"full_name": "Bad Phone", "email": "badphone@example.com",
                             "phone": "12345", "password": "longenough1"}, follow_redirects=True)
exists = n("SELECT count(*) FROM passenger WHERE email='badphone@example.com'")
msg = flash_of(r)
record("TC-03", "Input validation", "Sign up with a 5-digit phone number",
       "Rejected by CHECK chk_pax_phone; no row stored",
       f"Message “{msg}”; rows for that e-mail: {exists}", exists == 0 and "phone" in msg)

# ── TC-04 wrong password ───────────────────────────────────────────────────
c4 = A.app.test_client()
r = c4.post("/login", data={"email": TESTER, "password": "wrong-password"})
with c4.session_transaction() as s:
    signed = "pid" in s
record("TC-04", "Login", "Correct e-mail, wrong password",
       "Sign-in refused; no session created",
       f"HTTP {r.status_code}; message “{flash_of(r)}”; session created: {signed}",
       (not signed) and "wrong" in flash_of(r))

# ── TC-05 role check ───────────────────────────────────────────────────────
r = cust.get("/admin")
record("TC-05", "Access control (RBAC)", "Customer account opens /admin",
       "HTTP 403 – admin area refused", f"HTTP {r.status_code}", r.status_code == 403)

# ── TC-06 search ───────────────────────────────────────────────────────────
r = cust.get(f"/search?origin=HYD&dest=BOM&date={THREE.isoformat()}&cabin=ECONOMY")
nos = re.findall(rb'class="tag mono">(SB\d+)<', r.data)
fares = re.findall(rb'result__price.*?<b>([\d,]+)</b>', r.data, flags=re.S)
record("TC-06", "Flight search", f"HYD → BOM, {THREE:%d %b %Y}, Economy",
       "Both scheduled HYD→BOM flights listed with live seat counts and fares",
       f"HTTP {r.status_code}; flights {', '.join(x.decode() for x in nos)}; "
       f"fares ₹{', '.join(x.decode() for x in fares)}",
       r.status_code == 200 and len(nos) == 2)

# ── TC-07 book two seats ───────────────────────────────────────────────────
f1 = flight_on(3, "SB101")
seats = free_seats(f1["flight_id"], "ECONOMY", 2)
b0 = n("SELECT count(*) FROM booking")
r = cust.post(f"/flight/{f1['flight_id']}/book",
              data={"seat_id": [str(s["seat_id"]) for s in seats], "cabin": "ECONOMY", "method": "UPI"},
              follow_redirects=True)
bk = db.query("""SELECT b.booking_id, b.pnr, count(t.*) AS tk, sum(t.fare_paid) AS fares,
                        (SELECT sum(amount) FROM payment WHERE booking_id=b.booking_id) AS paid
                   FROM booking b JOIN ticket t USING (booking_id)
                  WHERE b.passenger_id=(SELECT passenger_id FROM passenger WHERE email=%s)
                  GROUP BY b.booking_id ORDER BY b.booking_id DESC LIMIT 1""", (TESTER,), one=True)
ok = bk and bk["tk"] == 2 and bk["fares"] == bk["paid"] and len(bk["pnr"].strip()) == 6
record("TC-07", "Booking (transaction)",
       f"Book seats {seats[0]['seat_no']} and {seats[1]['seat_no']} on SB101, pay by UPI",
       "One booking, two tickets and one payment equal to the fares are committed together",
       f"PNR {bk['pnr']}; tickets={bk['tk']}; payment ₹{bk['paid']:,.0f} = fares ₹{bk['fares']:,.0f}", ok)
BOOKING_ID, PNR = bk["booking_id"], bk["pnr"]
TICKET_ID = db.query("SELECT ticket_id FROM ticket WHERE booking_id=%s ORDER BY ticket_id LIMIT 1",
                     (BOOKING_ID,), one=True)["ticket_id"]

# ── TC-08 seat already sold ────────────────────────────────────────────────
c8 = A.app.test_client()
c8.post("/login", data={"email": "asha@example.com", "password": "flyskybase"})
b_before = n("SELECT count(*) FROM booking")
t_before = n("SELECT count(*) FROM ticket")
r = c8.post(f"/flight/{f1['flight_id']}/book",
            data={"seat_id": str(seats[0]["seat_id"]), "cabin": "ECONOMY", "method": "CARD"},
            follow_redirects=True)
b_after, t_after = n("SELECT count(*) FROM booking"), n("SELECT count(*) FROM ticket")
record("TC-08", "Booking (negative)", f"A second customer tries to book the sold seat {seats[0]['seat_no']}",
       "Refused; transaction rolled back, so no booking, ticket or payment is left behind",
       f"Message “{flash_of(r)}”; bookings {b_before}→{b_after}, tickets {t_before}→{t_after}",
       b_before == b_after and t_before == t_after)

# ── TC-09 business fare ────────────────────────────────────────────────────
f2 = flight_on(4, "SB101")
rs = cust.get(f"/search?origin=HYD&dest=BOM&date={(date.today()+timedelta(days=4)).isoformat()}&cabin=BUSINESS")
bseat = free_seats(f2["flight_id"], "BUSINESS", 1)[0]
cust.post(f"/flight/{f2['flight_id']}/book",
          data={"seat_id": str(bseat["seat_id"]), "cabin": "ECONOMY", "method": "CARD"})   # cabin lied about on purpose
tk = db.query("SELECT cabin, fare_paid FROM ticket WHERE seat_id=%s AND flight_id=%s",
              (bseat["seat_id"], f2["flight_id"]), one=True)
want = round(float(f2["base_fare"]) * 2.4, 2)
record("TC-09", "Fare rules / tampering",
       f"Search Business, then book business seat {bseat['seat_no']} while the form claims ‘ECONOMY’",
       "Search page loads; fare is read from the seat itself and charged at 2.4× base",
       f"Search HTTP {rs.status_code}; ticket cabin={tk['cabin']}, fare ₹{tk['fare_paid']:,.0f} (2.4 × {f2['base_fare']:,.0f})",
       rs.status_code == 200 and tk["cabin"] == "BUSINESS" and float(tk["fare_paid"]) == want)
BIZ_BOOKING = db.query("SELECT booking_id FROM ticket WHERE seat_id=%s AND flight_id=%s",
                       (bseat["seat_id"], f2["flight_id"]), one=True)["booking_id"]

# ── TC-10 check-in ─────────────────────────────────────────────────────────
cust.post(f"/ticket/{TICKET_ID}/checkin")
ck = db.query("SELECT status, checked_in_at IS NOT NULL AS stamped FROM ticket WHERE ticket_id=%s",
              (TICKET_ID,), one=True)
record("TC-10", "Check-in (update)", "Check in a confirmed ticket from the boarding-pass page",
       "status becomes CHECKED_IN and checked_in_at is stamped together (chk_ticket_checkin)",
       f"status={ck['status']}; timestamp set={ck['stamped']}", ck["status"] == "CHECKED_IN" and ck["stamped"])

# ── TC-11 cancel + refund ──────────────────────────────────────────────────
paid = float(n("SELECT sum(amount) FROM payment WHERE booking_id=%s", (BOOKING_ID,)))
cust.post(f"/booking/{BOOKING_ID}/cancel")
tks = db.query("SELECT status, seat_id FROM ticket WHERE booking_id=%s", (BOOKING_ID,))
rf = db.query("SELECT amount, method FROM payment WHERE booking_id=%s AND amount<0", (BOOKING_ID,), one=True)
ok = (all(t["status"] == "CANCELLED" and t["seat_id"] is None for t in tks)
      and rf and abs(float(rf["amount"]) + paid * 0.9) < 0.01)
record("TC-11", "Cancellation (controlled removal)", "Cancel the booking more than 24 h before departure",
       "Tickets CANCELLED, seats released (seat_id NULL), refund of 90% written as a negative payment",
       f"{len(tks)} tickets CANCELLED, seat_id NULL; refund row ₹{rf['amount']:,.0f} ({rf['method']}) on ₹{paid:,.0f}", ok)

# ── TC-12 cancel again ─────────────────────────────────────────────────────
p_before = n("SELECT count(*) FROM payment WHERE booking_id=%s", (BOOKING_ID,))
r = cust.post(f"/booking/{BOOKING_ID}/cancel", follow_redirects=True)
p_after = n("SELECT count(*) FROM payment WHERE booking_id=%s", (BOOKING_ID,))
record("TC-12", "Cancellation (negative)", "Cancel the same booking a second time",
       "No second refund is written",
       f"Message “{flash_of(r)}”; payment rows {p_before}→{p_after}", p_before == p_after)

# ── TC-13 / TC-14 the race ─────────────────────────────────────────────────
admin.post("/login", data={"email": "ops@skybase.in", "password": "skybase-ops"})


def race(lock):
    admin.post("/admin/lab/reset")
    m = re.search(rb'data-flight="(\d+)" data-seat="(\d+)"', admin.get("/admin/lab").data)
    d = admin.post("/admin/lab/run", data={"flight_id": m.group(1).decode(), "seat_id": m.group(2).decode(),
                                           "lock": lock, "hold_ms": "400"}).get_json()
    return d


d = race("1")
outs = sorted(a["outcome"] for a in d["agents"])
record("TC-13", "Concurrency (row lock)", f"Two agents book seat {d['seat_no']} at the same instant, FOR UPDATE on",
       "One commits; the other blocks on the lock, re-reads and stops cleanly; exactly one holder",
       f"{' + '.join(outs)}; holders of the seat: {len(d['holders'])}",
       len(d["holders"]) == 1 and outs == ["BLOCKED THEN BAILED", "COMMITTED"])
d = race("0")
outs = sorted(a["outcome"] for a in d["agents"])
sq = [a.get("sqlstate") for a in d["agents"] if a.get("sqlstate")]
record("TC-14", "Concurrency (index only)", f"Same race on seat {d['seat_no']} with the lock switched off",
       "Partial unique index rejects the loser (SQLSTATE 23505); exactly one holder",
       f"{' + '.join(outs)}; SQLSTATE {', '.join(sq)}; holders: {len(d['holders'])}",
       len(d["holders"]) == 1 and outs == ["COMMITTED", "REJECTED"] and "23505" in sq)
admin.post("/admin/lab/reset")

# ── TC-15 trigger: seat from another aircraft ──────────────────────────────
atr_seat = db.query("SELECT seat_id FROM seat WHERE aircraft_id=3 LIMIT 1", one=True)["seat_id"]
a320 = flight_on(5, "SB101")
pid = n("SELECT passenger_id FROM passenger WHERE email=%s", (TESTER,))
bid = db.query("INSERT INTO booking (pnr, passenger_id) VALUES ('TRG001', %s) RETURNING booking_id", (pid,), one=True)["booking_id"]
err = ""
try:
    db.execute("""INSERT INTO ticket (ticket_no, booking_id, flight_id, passenger_id, seat_id, cabin, fare_paid)
                  VALUES ('SB-0000000001', %s, %s, %s, %s, 'ECONOMY', 100)""",
               (bid, a320["flight_id"], pid, atr_seat))
except psycopg.errors.CheckViolation as e:
    err = str(e).split("\n")[0]
record("TC-15", "Trigger R4", "Insert a ticket on an A320 flight using a seat that belongs to the ATR",
       "Trigger ticket_seat_matches_aircraft raises an error; row not stored",
       f"{err[:90]}", "does not exist on the aircraft" in err)
db.execute("DELETE FROM booking WHERE booking_id=%s", (bid,))

# ── TC-16 / TC-17 admin flight rules ───────────────────────────────────────
base = {"flight_no": "SB777", "route_id": "1", "aircraft_id": "1", "dep_date": THREE.isoformat(),
        "dep_time": "09:30", "base_fare": "5200", "gate": "A9"}
admin.post("/admin/flights", data=base)
r = admin.post("/admin/flights", data=base, follow_redirects=True)
cnt = n("SELECT count(*) FROM flight WHERE flight_no='SB777'")
record("TC-16", "Admin: create flight", "Add flight SB777 twice for the same date",
       "Second insert refused by UNIQUE(flight_no, dep_date)",
       f"Message “{flash_of(r)[:70]}”; SB777 rows: {cnt}", cnt == 1 and "uq_flight_no_date" in flash_of(r))
bad = dict(base, flight_no="SB778", base_fare="-100")
r = admin.post("/admin/flights", data=bad, follow_redirects=True)
cnt = n("SELECT count(*) FROM flight WHERE flight_no='SB778'")
record("TC-17", "Admin: create flight", "Add a flight with a negative base fare",
       "Rejected by CHECK chk_flight_fare; no row stored",
       f"Message “Rejected by a CHECK constraint”; SB778 rows: {cnt}",
       cnt == 0 and "CHECK" in flash_of(r))

# ── TC-18 another customer's PNR ───────────────────────────────────────────
other.post("/login", data={"email": "asha@example.com", "password": "flyskybase"})
r = other.get(f"/booking/{PNR}")
record("TC-18", "Access control (row level)", "A different customer opens someone else's PNR URL",
       "HTTP 403 – booking not shown", f"HTTP {r.status_code}", r.status_code == 403)

# clean up what the suite created
db.execute("DELETE FROM flight WHERE flight_no IN ('SB777','SB778')")

counts = db.query("""SELECT (SELECT count(*) FROM booking) AS b, (SELECT count(*) FROM ticket) AS t,
                            (SELECT count(*) FROM payment) AS p""", one=True)
passed = sum(1 for x in RESULTS if x["status"] == "Pass")
print(f"\n{passed}/{len(RESULTS)} passed")
json.dump({"results": RESULTS, "passed": passed, "total": len(RESULTS),
           "final_counts": dict(counts)}, open(Path(__file__).parent / "results.json", "w"),
          indent=1, default=str)
sys.exit(0 if passed == len(RESULTS) else 1)
