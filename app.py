"""
SkyBase — Airline Reservation & Flight Operations
DBMS Project-Based Learning · Project 18

Flask + PostgreSQL, raw SQL throughout.  Two faces:

    /        the customer side — search, seat map, book, boarding pass
    /admin   the operations side — flights, manifest, reports, concurrency lab

Access control is a single `role` column on PASSENGER, checked by the
@admin_only decorator.  Two roles, not seven: the Review 1 RBAC tables were
cut along with the rest of the model to keep the ER diagram small.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps

import psycopg
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db

try:
    from dotenv import load_dotenv
    # override=False on purpose: a real environment variable always wins over
    # a dotenv file. On Render the config comes from the dashboard, and a
    # stray .env must never be able to silently override it.
    load_dotenv(".env.local", override=False)
    load_dotenv(".env", override=False)
except ImportError:
    pass

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "dev-only-not-for-render")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True


# ───────────────────────────────────────────────────────────── auth plumbing


def current_user():
    pid = session.get("pid")
    if not pid:
        return None
    return db.query(
        "SELECT passenger_id, email, full_name, phone, role FROM passenger"
        " WHERE passenger_id = %s",
        (pid,), one=True,
    )


@app.context_processor
def inject_globals():
    return {"me": current_user(), "now": datetime.now()}


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("pid"):
            flash("Sign in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def admin_only(fn):
    """RBAC, such as it is: one role column, one check, every admin route."""
    @wraps(fn)
    def wrapper(*a, **kw):
        me = current_user()
        if not me:
            return redirect(url_for("login", next=request.path))
        if me["role"] != "ADMIN":
            abort(403)
        return fn(*a, **kw)
    return wrapper


# ───────────────────────────────────────────────────────── jinja formatting


@app.template_filter("rupees")
def rupees(v):
    if v is None:
        return "—"
    return f"{float(v):,.0f}"


@app.template_filter("hhmm")
def hhmm(dt):
    return dt.strftime("%H:%M") if dt else "—"


@app.template_filter("daymon")
def daymon(dt):
    return dt.strftime("%a %d %b") if dt else "—"


@app.template_filter("dur")
def dur(minutes):
    if minutes is None:
        return "—"
    m = int(minutes)
    return f"{m // 60}h {m % 60:02d}m"


# ══════════════════════════════════════════════════════════ CUSTOMER SIDE ══


@app.route("/")
def home():
    airports = db.query(
        "SELECT airport_id, iata_code, name, city FROM airport ORDER BY iata_code"
    )
    # The six fixed routes, with the cheapest fare currently on sale for each.
    routes = db.query(
        """
        SELECT r.route_id, o.iata_code AS origin, d.iata_code AS destination,
               o.city AS from_city, d.city AS to_city,
               r.distance_km, r.block_min,
               min(f.base_fare) AS from_fare,
               count(f.flight_id) AS flights
          FROM route r
          JOIN airport o ON o.airport_id = r.origin_id
          JOIN airport d ON d.airport_id = r.dest_id
          LEFT JOIN flight f ON f.route_id = r.route_id
                            AND f.sched_dep > CURRENT_TIMESTAMP
                            AND f.status = 'SCHEDULED'
         GROUP BY r.route_id, o.iata_code, d.iata_code, o.city, d.city,
                  r.distance_km, r.block_min
         ORDER BY r.route_id
        """
    )
    # The live departure board — next eight departures, any route.
    board = db.query(
        """
        SELECT f.flight_id, f.flight_no, f.sched_dep, f.gate, f.status,
               o.iata_code AS origin, d.iata_code AS destination, d.city AS to_city,
               av.seats_free
          FROM flight f
          JOIN route   r ON r.route_id   = f.route_id
          JOIN airport o ON o.airport_id = r.origin_id
          JOIN airport d ON d.airport_id = r.dest_id
          JOIN v_seat_availability av ON av.flight_id = f.flight_id
         WHERE f.sched_dep > CURRENT_TIMESTAMP
         ORDER BY f.sched_dep
         LIMIT 8
        """
    )
    return render_template("home.html", airports=airports, routes=routes,
                           board=board, today=date.today().isoformat())


@app.route("/search")
def search():
    origin = request.args.get("origin", "")
    dest = request.args.get("dest", "")
    when = request.args.get("date", "") or date.today().isoformat()
    cabin = request.args.get("cabin", "ECONOMY")
    pax = max(1, min(6, int(request.args.get("pax", 1) or 1)))

    airports = db.query("SELECT iata_code, city FROM airport ORDER BY iata_code")

    flights = []
    if origin and dest and origin != dest:
        flights = db.query(
            """
            SELECT f.flight_id, f.flight_no, f.sched_dep, f.sched_arr,
                   f.base_fare, f.status, f.gate,
                   o.iata_code AS origin, d.iata_code AS destination,
                   o.city AS from_city, d.city AS to_city,
                   r.block_min, r.distance_km,
                   ac.model, ac.tail_number,
                   av.capacity, av.seats_free,
                   (SELECT count(*) FROM seat s
                     WHERE s.aircraft_id = f.aircraft_id
                       AND s.cabin = %s
                       AND NOT EXISTS (SELECT 1 FROM ticket t
                                        WHERE t.seat_id = s.seat_id
                                          AND t.flight_id = f.flight_id
                                          AND t.status <> 'CANCELLED')
                   ) AS cabin_free
              FROM flight   f
              JOIN route    r  ON r.route_id   = f.route_id
              JOIN airport  o  ON o.airport_id = r.origin_id
              JOIN airport  d  ON d.airport_id = r.dest_id
              JOIN aircraft ac ON ac.aircraft_id = f.aircraft_id
              JOIN v_seat_availability av ON av.flight_id = f.flight_id
             WHERE o.iata_code = %s
               AND d.iata_code = %s
               AND f.dep_date  = %s
               AND f.status IN ('SCHEDULED', 'DELAYED')
             ORDER BY f.sched_dep
            """,
            (cabin, origin, dest, when),
        )

    return render_template("search.html", airports=airports, flights=flights,
                           origin=origin, dest=dest, when=when, cabin=cabin,
                           pax=pax, today=date.today().isoformat())


@app.route("/flight/<int:flight_id>")
@login_required
def flight_detail(flight_id):
    cabin = request.args.get("cabin", "ECONOMY")
    pax = max(1, min(6, int(request.args.get("pax", 1) or 1)))

    flight = db.query(
        """
        SELECT f.flight_id, f.flight_no, f.sched_dep, f.sched_arr, f.base_fare,
               f.gate, f.status, f.aircraft_id,
               o.iata_code AS origin, d.iata_code AS destination,
               o.name AS from_name, d.name AS to_name,
               o.city AS from_city, d.city AS to_city,
               r.block_min, r.distance_km, ac.model, ac.tail_number
          FROM flight   f
          JOIN route    r  ON r.route_id   = f.route_id
          JOIN airport  o  ON o.airport_id = r.origin_id
          JOIN airport  d  ON d.airport_id = r.dest_id
          JOIN aircraft ac ON ac.aircraft_id = f.aircraft_id
         WHERE f.flight_id = %s
        """,
        (flight_id,), one=True,
    )
    if not flight:
        abort(404)

    # The seat map: every seat on the airframe, with whether this flight has
    # sold it.  NOT EXISTS rather than a LEFT JOIN because we only want a
    # boolean, and it reads closer to the business question.
    seats = db.query(
        """
        SELECT s.seat_id, s.seat_no, s.cabin, s.is_exit_row,
               substring(s.seat_no from '^[0-9]+')::int AS row_no,
               right(s.seat_no, 1) AS col,
               EXISTS (SELECT 1 FROM ticket t
                        WHERE t.seat_id = s.seat_id
                          AND t.flight_id = %s
                          AND t.status <> 'CANCELLED') AS taken
          FROM seat s
         WHERE s.aircraft_id = %s
         ORDER BY row_no, col
        """,
        (flight_id, flight["aircraft_id"]),
    )

    rows: dict[int, list] = {}
    for s in seats:
        rows.setdefault(s["row_no"], []).append(s)

    return render_template("flight.html", flight=flight, rows=sorted(rows.items()),
                           cabin=cabin, pax=pax,
                           unit_fare=db.fare_for(flight["base_fare"], cabin))


@app.post("/flight/<int:flight_id>/book")
@login_required
def book(flight_id):
    me = current_user()
    seat_ids = [int(s) for s in request.form.getlist("seat_id") if s]
    cabin = request.form.get("cabin", "ECONOMY")
    method = request.form.get("method", "CARD")

    if not seat_ids:
        flash("Pick at least one seat.", "warn")
        return redirect(url_for("flight_detail", flight_id=flight_id, cabin=cabin))

    try:
        result = db.book_seats(me["passenger_id"], flight_id, seat_ids, cabin, method)
    except db.SeatUnavailable as e:
        flash(f"Seat {', '.join(e.seat_nos)} was taken while you were paying. "
              "Pick another.", "error")
        return redirect(url_for("flight_detail", flight_id=flight_id, cabin=cabin))
    except psycopg.errors.UniqueViolation:
        # The partial unique index did its job.
        flash("That seat has just been sold. Pick another.", "error")
        return redirect(url_for("flight_detail", flight_id=flight_id, cabin=cabin))
    except psycopg.errors.CheckViolation as e:
        flash(str(e).split("\n")[0], "error")
        return redirect(url_for("flight_detail", flight_id=flight_id, cabin=cabin))

    flash(f"Booked. PNR {result['pnr']}.", "ok")
    return redirect(url_for("boarding_pass", pnr=result["pnr"]))


@app.route("/booking/<pnr>")
@login_required
def boarding_pass(pnr):
    me = current_user()
    booking = db.query(
        """
        SELECT b.booking_id, b.pnr, b.booked_at, b.status, b.passenger_id,
               COALESCE(sum(t.fare_paid) FILTER (WHERE t.status <> 'CANCELLED'), 0) AS total,
               COALESCE((SELECT sum(amount) FROM payment WHERE booking_id = b.booking_id), 0) AS paid
          FROM booking b
          LEFT JOIN ticket t ON t.booking_id = b.booking_id
         WHERE b.pnr = %s
         GROUP BY b.booking_id
        """,
        (pnr.upper(),), one=True,
    )
    if not booking:
        abort(404)
    if booking["passenger_id"] != me["passenger_id"] and me["role"] != "ADMIN":
        abort(403)

    tickets = db.query(
        """
        SELECT t.ticket_id, t.ticket_no, t.cabin, t.fare_paid, t.status,
               t.checked_in_at, s.seat_no, s.is_exit_row,
               p.full_name,
               f.flight_id, f.flight_no, f.sched_dep, f.sched_arr, f.gate,
               f.status AS flight_status,
               o.iata_code AS origin, d.iata_code AS destination,
               o.city AS from_city, d.city AS to_city,
               o.name AS from_name, d.name AS to_name, r.block_min
          FROM ticket t
          JOIN flight  f ON f.flight_id  = t.flight_id
          JOIN route   r ON r.route_id   = f.route_id
          JOIN airport o ON o.airport_id = r.origin_id
          JOIN airport d ON d.airport_id = r.dest_id
          JOIN passenger p ON p.passenger_id = t.passenger_id
          LEFT JOIN seat s ON s.seat_id = t.seat_id
         WHERE t.booking_id = %s
         ORDER BY f.sched_dep, s.seat_no
        """,
        (booking["booking_id"],),
    )
    return render_template("booking.html", booking=booking, tickets=tickets)


@app.route("/trips")
@login_required
def trips():
    me = current_user()
    bookings = db.query(
        """
        SELECT b.booking_id, b.pnr, b.booked_at, b.status,
               count(t.ticket_id)                      AS pax,
               min(f.sched_dep)                        AS departs,
               string_agg(DISTINCT o.iata_code || '→' || d.iata_code, ', ') AS legs,
               COALESCE(sum(t.fare_paid) FILTER (WHERE t.status <> 'CANCELLED'), 0) AS total
          FROM booking b
          LEFT JOIN ticket  t ON t.booking_id = b.booking_id
          LEFT JOIN flight  f ON f.flight_id  = t.flight_id
          LEFT JOIN route   r ON r.route_id   = f.route_id
          LEFT JOIN airport o ON o.airport_id = r.origin_id
          LEFT JOIN airport d ON d.airport_id = r.dest_id
         WHERE b.passenger_id = %s
         GROUP BY b.booking_id
         ORDER BY b.booked_at DESC
        """,
        (me["passenger_id"],),
    )
    return render_template("trips.html", bookings=bookings)


@app.post("/booking/<int:booking_id>/cancel")
@login_required
def cancel(booking_id):
    me = current_user()
    try:
        pid = None if me["role"] == "ADMIN" else me["passenger_id"]
        res = db.cancel_booking(booking_id, pid)
    except PermissionError:
        abort(403)
    except ValueError:
        abort(404)
    if res.get("already"):
        flash("That booking was already cancelled.", "info")
    else:
        flash(f"Cancelled. ₹{res['refund']:,.0f} refunded to source.", "ok")
    return redirect(url_for("trips"))


@app.post("/ticket/<int:ticket_id>/checkin")
@login_required
def do_check_in(ticket_id):
    me = current_user()
    pid = None if me["role"] == "ADMIN" else me["passenger_id"]
    try:
        res = db.check_in(ticket_id, pid)
    except PermissionError:
        abort(403)
    except ValueError:
        abort(404)
    flash("Checked in. Boarding pass issued." if res["ok"]
          else f"Cannot check in: {res['why']}.",
          "ok" if res["ok"] else "warn")
    return redirect(request.referrer or url_for("trips"))


# ═════════════════════════════════════════════════════════════════ AUTH ══


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("full_name", "").strip()
        phone = (request.form.get("phone", "").strip() or None)
        pw = request.form.get("password", "")

        if len(pw) < 8:
            flash("Password needs at least 8 characters.", "warn")
            return render_template("signup.html", email=email, full_name=name, phone=phone)
        try:
            row = db.query(
                "INSERT INTO passenger (email, password_hash, full_name, phone)"
                " VALUES (%s, %s, %s, %s) RETURNING passenger_id",
                (email, generate_password_hash(pw), name, phone), one=True,
            )
        except psycopg.errors.UniqueViolation:
            flash("That email already has an account. Sign in instead.", "warn")
            return redirect(url_for("login", email=email))
        except psycopg.errors.CheckViolation:
            # The database rejected it, so the message is honest about which rule.
            flash("Check the email format and that the phone is 10 digits.", "warn")
            return render_template("signup.html", email=email, full_name=name, phone=phone)

        session["pid"] = row["passenger_id"]
        flash("Welcome aboard.", "ok")
        return redirect(url_for("home"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        row = db.query(
            "SELECT passenger_id, password_hash, role FROM passenger WHERE email = %s",
            (email,), one=True,
        )
        if not row or not check_password_hash(row["password_hash"], pw):
            flash("Email or password is wrong.", "error")
            return render_template("login.html", email=email)
        session["pid"] = row["passenger_id"]
        nxt = request.args.get("next")
        if nxt and nxt.startswith("/"):
            return redirect(nxt)
        return redirect(url_for("admin_home" if row["role"] == "ADMIN" else "home"))
    return render_template("login.html", email=request.args.get("email", ""))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ════════════════════════════════════════════════════════════════ ADMIN ══


@app.route("/admin")
@admin_only
def admin_home():
    kpi = db.query(
        """
        SELECT
          (SELECT count(*) FROM flight WHERE dep_date = CURRENT_DATE)      AS flights_today,
          (SELECT count(*) FROM booking WHERE booked_at::date = CURRENT_DATE
                                          AND status = 'CONFIRMED')        AS bookings_today,
          (SELECT count(*) FROM ticket  WHERE status <> 'CANCELLED')       AS live_tickets,
          (SELECT count(*) FROM passenger WHERE role = 'CUSTOMER')         AS customers,
          (SELECT COALESCE(sum(amount), 0) FROM payment)                   AS net_revenue,
          (SELECT ROUND(AVG(load_factor_pct), 1) FROM v_flight_occupancy
            WHERE dep_date BETWEEN CURRENT_DATE - 3 AND CURRENT_DATE)      AS avg_load
        """,
        one=True,
    )
    departures = db.query(
        """
        SELECT flight_id, flight_no, sched_dep, origin, destination, status,
               capacity, seats_sold, load_factor_pct, gross_revenue
          FROM v_flight_occupancy
         WHERE dep_date = CURRENT_DATE
         ORDER BY sched_dep
        """
    )
    recent = db.query(
        """
        SELECT b.pnr, b.booked_at, b.status, p.full_name, p.email,
               count(t.ticket_id) AS pax,
               COALESCE(sum(t.fare_paid), 0) AS total,
               string_agg(DISTINCT f.flight_no, ' ') AS flights
          FROM booking b
          JOIN passenger p ON p.passenger_id = b.passenger_id
          LEFT JOIN ticket t ON t.booking_id = b.booking_id
          LEFT JOIN flight f ON f.flight_id = t.flight_id
         GROUP BY b.booking_id, p.full_name, p.email
         ORDER BY b.booked_at DESC
         LIMIT 8
        """
    )
    return render_template("admin/overview.html", kpi=kpi,
                           departures=departures, recent=recent)


@app.route("/admin/flights")
@admin_only
def admin_flights():
    when = request.args.get("date", "") or date.today().isoformat()
    flights = db.query(
        """
        SELECT o.flight_id, o.flight_no, o.sched_dep, o.origin, o.destination,
               o.status, o.tail_number, o.model, o.capacity, o.seats_sold,
               o.load_factor_pct, o.gross_revenue, f.gate, f.base_fare, f.sched_arr
          FROM v_flight_occupancy o
          JOIN flight f ON f.flight_id = o.flight_id
         WHERE o.dep_date = %s
         ORDER BY o.sched_dep
        """,
        (when,),
    )
    routes = db.query(
        """
        SELECT r.route_id, o.iata_code || ' → ' || d.iata_code AS pair, r.block_min
          FROM route r
          JOIN airport o ON o.airport_id = r.origin_id
          JOIN airport d ON d.airport_id = r.dest_id
         ORDER BY r.route_id
        """
    )
    aircraft = db.query(
        """
        SELECT a.aircraft_id, a.tail_number, a.model, count(s.seat_id) AS seats
          FROM aircraft a LEFT JOIN seat s ON s.aircraft_id = a.aircraft_id
         GROUP BY a.aircraft_id ORDER BY a.aircraft_id
        """
    )
    return render_template("admin/flights.html", flights=flights, routes=routes,
                           aircraft=aircraft, when=when)


@app.post("/admin/flights")
@admin_only
def admin_create_flight():
    f = request.form
    try:
        dep = datetime.fromisoformat(f"{f['dep_date']}T{f['dep_time']}")
        route = db.query("SELECT block_min FROM route WHERE route_id = %s",
                         (f["route_id"],), one=True)
        arr = dep + timedelta(minutes=route["block_min"])
        db.execute(
            """
            INSERT INTO flight (flight_no, route_id, aircraft_id, sched_dep,
                                sched_arr, base_fare, gate)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (f["flight_no"].upper(), f["route_id"], f["aircraft_id"], dep, arr,
             f["base_fare"], f.get("gate") or None),
        )
        flash(f"Flight {f['flight_no'].upper()} added.", "ok")
    except psycopg.errors.UniqueViolation:
        flash(f"{f['flight_no'].upper()} already exists on that date — "
              "uq_flight_no_date refused it.", "error")
    except psycopg.errors.CheckViolation as e:
        flash("Rejected by a CHECK constraint: "
              + str(e).split("\n")[0].replace('new row for relation "flight" ', ""),
              "error")
    except (KeyError, ValueError):
        flash("Fill in every field.", "warn")
    return redirect(url_for("admin_flights", date=f.get("dep_date")))


@app.post("/admin/flights/<int:flight_id>/status")
@admin_only
def admin_flight_status(flight_id):
    status = request.form.get("status", "SCHEDULED")
    try:
        db.execute("UPDATE flight SET status = %s WHERE flight_id = %s",
                   (status, flight_id))
        flash(f"Flight set to {status.lower()}.", "ok")
    except psycopg.errors.CheckViolation:
        flash("Not a valid status.", "error")
    return redirect(request.referrer or url_for("admin_flights"))


@app.route("/admin/flights/<int:flight_id>/manifest")
@admin_only
def admin_manifest(flight_id):
    flight = db.query(
        """
        SELECT o.*, f.gate, f.sched_arr, f.base_fare
          FROM v_flight_occupancy o JOIN flight f USING (flight_id)
         WHERE o.flight_id = %s
        """,
        (flight_id,), one=True,
    )
    if not flight:
        abort(404)
    pax = db.query(
        """
        SELECT seat_no, cabin, full_name, phone, pnr, ticket_no,
               status, checked_in_at
          FROM v_flight_manifest
         WHERE flight_id = %s
         ORDER BY substring(seat_no from '^[0-9]+')::int, seat_no
        """,
        (flight_id,),
    )
    return render_template("admin/manifest.html", flight=flight, pax=pax,
                           sql=SQL_SHOWN["manifest"])


@app.route("/admin/reports")
@admin_only
def admin_reports():
    occupancy = db.query(
        """
        SELECT flight_no, dep_date, origin, destination, model,
               capacity, seats_sold, load_factor_pct, gross_revenue
          FROM v_flight_occupancy
         WHERE dep_date BETWEEN CURRENT_DATE - 3 AND CURRENT_DATE + 9
           AND seats_sold > 0
         ORDER BY load_factor_pct DESC, dep_date
         LIMIT 12
        """
    )
    demand = db.query(
        """
        SELECT pair, from_city, to_city, distance_km, flights_operated,
               passengers, revenue, avg_fare
          FROM v_route_demand
         ORDER BY revenue DESC
        """
    )
    revenue = db.query(
        "SELECT day, gross, refunds, net, payments FROM v_revenue_daily"
        " ORDER BY day DESC LIMIT 10"
    )
    cabins = db.query(
        """
        SELECT t.cabin,
               count(*)                       AS tickets,
               sum(t.fare_paid)               AS revenue,
               ROUND(avg(t.fare_paid), 2)     AS avg_fare
          FROM ticket t
         WHERE t.status <> 'CANCELLED'
         GROUP BY t.cabin
         ORDER BY revenue DESC NULLS LAST
        """
    )
    return render_template("admin/reports.html", occupancy=occupancy,
                           demand=demand, revenue=revenue, cabins=cabins,
                           sql=SQL_SHOWN)


# ──────────────────────────────────────────────────── the concurrency lab


@app.route("/admin/lab")
@admin_only
def admin_lab():
    """The Review 1 centrepiece, made real: two agents, one seat."""
    flight = db.query(
        """
        SELECT f.flight_id, f.flight_no, f.sched_dep,
               o.iata_code AS origin, d.iata_code AS destination,
               av.seats_free, av.capacity
          FROM flight f
          JOIN route   r ON r.route_id = f.route_id
          JOIN airport o ON o.airport_id = r.origin_id
          JOIN airport d ON d.airport_id = r.dest_id
          JOIN v_seat_availability av ON av.flight_id = f.flight_id
         WHERE f.sched_dep > CURRENT_TIMESTAMP AND f.status = 'SCHEDULED'
           AND av.seats_free > 2
         ORDER BY f.sched_dep
         LIMIT 1
        """,
        one=True,
    )
    seat = None
    if flight:
        seat = db.query(
            """
            SELECT s.seat_id, s.seat_no FROM seat s
              JOIN flight f ON f.aircraft_id = s.aircraft_id
             WHERE f.flight_id = %s
               AND NOT EXISTS (SELECT 1 FROM ticket t
                                WHERE t.seat_id = s.seat_id
                                  AND t.flight_id = f.flight_id
                                  AND t.status <> 'CANCELLED')
             ORDER BY substring(s.seat_no from '^[0-9]+')::int, s.seat_no
             LIMIT 1
            """,
            (flight["flight_id"],), one=True,
        )
    return render_template("admin/lab.html", flight=flight, seat=seat)


@app.post("/admin/lab/run")
@admin_only
def admin_lab_run():
    """Fire two real transactions at the same seat, from two threads, and
    report exactly what the database did to each of them."""
    flight_id = int(request.form["flight_id"])
    seat_id = int(request.form["seat_id"])
    use_lock = request.form.get("lock") == "1"
    hold_ms = int(request.form.get("hold_ms", 400))

    # Two distinct passengers, so the race reads as two customers rather than
    # one person double-clicking — the seat contention is the point, not identity.
    lab_pax = []
    for i in (1, 2):
        row = db.query("SELECT passenger_id FROM passenger WHERE email = %s",
                       (f"lab{i}@skybase.test",), one=True)
        if not row:
            row = db.query(
                "INSERT INTO passenger (email, password_hash, full_name)"
                " VALUES (%s, %s, %s) RETURNING passenger_id",
                (f"lab{i}@skybase.test", generate_password_hash(os.urandom(16).hex()),
                 f"Lab Agent {i}"), one=True,
            )
        lab_pax.append(row["passenger_id"])

    results: dict[int, dict] = {}
    t0 = time.perf_counter()

    def attempt(n: int, pid: int):
        started = time.perf_counter() - t0
        rec = {"agent": n, "started_ms": round(started * 1000, 1)}
        try:
            out = db.book_seats(pid, flight_id, [seat_id], "ECONOMY",
                                use_row_lock=use_lock, hold_ms=hold_ms)
            rec.update(outcome="COMMITTED", pnr=out["pnr"],
                       detail=f"ticket issued · PNR {out['pnr']}")
        except psycopg.errors.UniqueViolation as e:
            rec.update(outcome="REJECTED", sqlstate="23505",
                       detail="unique violation on uq_ticket_seat_per_flight — "
                              "the partial index refused the duplicate seat",
                       raw=str(e).split("\n")[0])
        except db.SeatUnavailable as e:
            rec.update(outcome="BLOCKED THEN BAILED", sqlstate="—",
                       detail=f"waited for the row lock, then read the committed "
                              f"truth: {', '.join(e.seat_nos)} was taken. "
                              f"No error, no duplicate.")
        except psycopg.errors.CheckViolation as e:
            rec.update(outcome="REJECTED", sqlstate="23514",
                       detail="a trigger refused it",
                       raw=str(e).split("\n")[0])
        except Exception as e:  # noqa: BLE001 — the lab reports whatever happens
            rec.update(outcome="ERROR", detail=type(e).__name__, raw=str(e)[:200])
        rec["finished_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        results[n] = rec

    threads = [threading.Thread(target=attempt, args=(i + 1, lab_pax[i]))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    seat_row = db.query("SELECT seat_no FROM seat WHERE seat_id = %s",
                        (seat_id,), one=True)
    holders = db.query(
        """
        SELECT t.ticket_no, p.full_name, b.pnr
          FROM ticket t
          JOIN booking b   ON b.booking_id = t.booking_id
          JOIN passenger p ON p.passenger_id = t.passenger_id
         WHERE t.flight_id = %s AND t.seat_id = %s AND t.status <> 'CANCELLED'
        """,
        (flight_id, seat_id),
    )
    return jsonify({
        "seat_no": seat_row["seat_no"] if seat_row else "?",
        "mode": "FOR UPDATE row lock" if use_lock else "no lock (unique index only)",
        "hold_ms": hold_ms,
        "agents": [results.get(1), results.get(2)],
        "holders": holders,
        "verdict": ("Exactly one ticket holds the seat — the invariant held."
                    if len(holders) == 1 else
                    f"{len(holders)} tickets hold the seat. The invariant BROKE."),
    })


@app.post("/admin/lab/reset")
@admin_only
def admin_lab_reset():
    """Release whatever the lab booked, so it can be run again."""
    n = db.execute(
        """
        UPDATE ticket SET status = 'CANCELLED', seat_id = NULL, checked_in_at = NULL
         WHERE passenger_id IN (SELECT passenger_id FROM passenger
                                 WHERE email LIKE 'lab%@skybase.test')
           AND status <> 'CANCELLED'
        """
    )
    db.execute(
        """
        UPDATE booking SET status = 'CANCELLED'
         WHERE passenger_id IN (SELECT passenger_id FROM passenger
                                 WHERE email LIKE 'lab%@skybase.test')
        """
    )
    return jsonify({"released": n})


# ─────────────────────────────────────────────── live constraint catalogue


@app.route("/admin/schema")
@admin_only
def admin_schema():
    """Read the constraints back out of the catalog, so what is shown is what
    the database is actually enforcing — not a copy of the DDL that may have
    drifted from it."""
    tables = db.query(
        """
        SELECT c.relname AS table_name,
               obj_description(c.oid)        AS comment,
               (SELECT count(*) FROM pg_attribute a
                 WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped)
                                             AS columns,
               COALESCE(s.n_live_tup, 0)     AS approx_rows
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
         WHERE n.nspname = 'public' AND c.relkind = 'r'
         ORDER BY c.relname
        """
    )
    constraints = db.query(
        """
        SELECT conrelid::regclass::text AS table_name,
               conname                  AS name,
               CASE contype WHEN 'p' THEN 'PRIMARY KEY'
                            WHEN 'f' THEN 'FOREIGN KEY'
                            WHEN 'u' THEN 'UNIQUE'
                            WHEN 'c' THEN 'CHECK'
                            ELSE contype::text END AS kind,
               pg_get_constraintdef(oid) AS definition
          FROM pg_constraint
         WHERE connamespace = 'public'::regnamespace
         ORDER BY conrelid::regclass::text,
                  array_position(ARRAY['p','u','f','c'], contype::text), conname
        """
    )
    indexes = db.query(
        """
        SELECT tablename AS table_name, indexname AS name, indexdef AS definition
          FROM pg_indexes
         WHERE schemaname = 'public' AND indexdef ILIKE '%WHERE%'
         ORDER BY tablename, indexname
        """
    )
    triggers = db.query(
        """
        SELECT c.relname AS table_name, t.tgname AS name,
               pg_get_triggerdef(t.oid) AS definition
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE NOT t.tgisinternal AND n.nspname = 'public'
         ORDER BY c.relname, t.tgname
        """
    )
    views = db.query(
        """
        SELECT viewname AS name FROM pg_views
         WHERE schemaname = 'public' ORDER BY viewname
        """
    )
    counts = {
        "tables": len(tables),
        "constraints": len(constraints),
        "triggers": len(triggers),
        "views": len(views),
        "partial": len(indexes),
    }
    grouped: dict[str, list] = {}
    for c in constraints:
        grouped.setdefault(c["table_name"], []).append(c)

    return render_template("admin/schema.html", tables=tables, grouped=grouped,
                           indexes=indexes, triggers=triggers, views=views,
                           counts=counts)


# The SQL shown on the reports page, so the queries are part of the deliverable
# rather than buried in the source.
SQL_SHOWN = {
    "manifest": """SELECT seat_no, cabin, full_name, phone, pnr, ticket_no, status
  FROM v_flight_manifest
 WHERE flight_id = %s
 ORDER BY substring(seat_no from '^[0-9]+')::int, seat_no;""",
    "occupancy": """-- capacity is COUNT(seat); no capacity column exists anywhere
SELECT flight_no, origin, destination, capacity, seats_sold,
       ROUND(100.0 * seats_sold / NULLIF(capacity,0), 1) AS load_factor_pct
  FROM v_flight_occupancy
 WHERE seats_sold > 0
 ORDER BY load_factor_pct DESC;""",
    "demand": """SELECT pair, count(DISTINCT f.flight_id) AS flights_operated,
       count(t.ticket_id) AS passengers, sum(t.fare_paid) AS revenue
  FROM route r
  LEFT JOIN flight f ON f.route_id  = r.route_id
  LEFT JOIN ticket t ON t.flight_id = f.flight_id AND t.status <> 'CANCELLED'
 GROUP BY r.route_id
 ORDER BY revenue DESC;""",
    "revenue": """-- a refund is a negative row, so net falls out of one SUM
SELECT paid_at::date AS day,
       sum(amount) FILTER (WHERE amount > 0) AS gross,
      -sum(amount) FILTER (WHERE amount < 0) AS refunds,
       sum(amount)                           AS net
  FROM payment GROUP BY paid_at::date ORDER BY day DESC;""",
    "cabins": """SELECT cabin, count(*) AS tickets, sum(fare_paid) AS revenue,
       ROUND(avg(fare_paid), 2) AS avg_fare
  FROM ticket WHERE status <> 'CANCELLED'
 GROUP BY cabin ORDER BY revenue DESC;""",
}


# ─────────────────────────────────────────────────────────────── errors


@app.errorhandler(403)
def e403(_):
    return render_template("error.html", code="403",
                           title="Not your flight deck",
                           msg="That area needs an operations role. "
                               "Your account is a customer account."), 403


@app.errorhandler(404)
def e404(_):
    return render_template("error.html", code="404",
                           title="No such record",
                           msg="That flight, booking or page is not in the database."), 404


@app.errorhandler(500)
def e500(_):
    return render_template("error.html", code="500",
                           title="Something broke on the apron",
                           msg="The database refused the request and the "
                               "transaction was rolled back. Nothing was half-written."), 500


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5001)))
