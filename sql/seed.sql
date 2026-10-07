-- ============================================================================
--  SKYBASE · seed data
--
--  Deliberately tiny, on the reviewer's instruction: three airports, six
--  routes, three airframes.  All of it is FIXED reference data — the
--  application never inserts, updates or deletes these rows.  Only FLIGHT
--  (admin) and BOOKING / TICKET / PAYMENT (customers) grow at runtime.
--
--  Flights are generated relative to CURRENT_DATE, so the demo always has
--  three days of history (for the reports) and ten days of future inventory
--  (for booking).  Re-running this file is safe: it truncates first.
--
--  Run:  psql "$DATABASE_URL" -f sql/seed.sql
-- ============================================================================

BEGIN;

TRUNCATE payment, ticket, booking, flight, seat, aircraft, route, airport
    RESTART IDENTITY CASCADE;

-- ───────────────────────────────────────────────────── 3 airports  (fixed)

INSERT INTO airport (airport_id, iata_code, name, city) VALUES
    (1, 'HYD', 'Rajiv Gandhi International',            'Hyderabad'),
    (2, 'BOM', 'Chhatrapati Shivaji Maharaj International', 'Mumbai'),
    (3, 'DEL', 'Indira Gandhi International',           'Delhi');

-- ───────────────────────────────────────────── 6 routes  (fixed, directed)
-- Every ordered pair of the three airports.

INSERT INTO route (route_id, origin_id, dest_id, distance_km, block_min) VALUES
    (1, 1, 2,  710,  95),   -- HYD -> BOM
    (2, 2, 1,  710,  95),   -- BOM -> HYD
    (3, 1, 3, 1270, 140),   -- HYD -> DEL
    (4, 3, 1, 1270, 140),   -- DEL -> HYD
    (5, 2, 3, 1150, 130),   -- BOM -> DEL
    (6, 3, 2, 1150, 130);   -- DEL -> BOM

-- ────────────────────────────────────────────────── 3 airframes  (fixed)
-- No capacity column: capacity is COUNT(seat), computed by v_seat_availability.

INSERT INTO aircraft (aircraft_id, tail_number, model) VALUES
    (1, 'VT-SBA', 'Airbus A320neo'),
    (2, 'VT-SBB', 'Airbus A320neo'),
    (3, 'VT-SBC', 'ATR 72-600');

-- ────────────────────────────────────────────────────────────── seat maps
-- Built with generate_series rather than 140 literal INSERTs.

-- A320neo (VT-SBA, VT-SBB) — business: rows 1-2, 2+2 layout (A C D F).
INSERT INTO seat (aircraft_id, seat_no, cabin, is_exit_row)
SELECT ac.id, r.n || s.letter, 'BUSINESS', FALSE
  FROM (VALUES (1), (2)) AS ac(id)
 CROSS JOIN generate_series(1, 2) AS r(n)
 CROSS JOIN (VALUES ('A'), ('C'), ('D'), ('F')) AS s(letter);

-- A320neo — economy: rows 10-17, 3+3 layout (A-F).  Row 10 is the exit row.
INSERT INTO seat (aircraft_id, seat_no, cabin, is_exit_row)
SELECT ac.id, r.n || s.letter, 'ECONOMY', r.n = 10
  FROM (VALUES (1), (2)) AS ac(id)
 CROSS JOIN generate_series(10, 17) AS r(n)
 CROSS JOIN (VALUES ('A'), ('B'), ('C'), ('D'), ('E'), ('F')) AS s(letter);

-- ATR 72-600 (VT-SBC) — all economy, rows 1-7, 2+2 layout.  Row 4 is the exit.
INSERT INTO seat (aircraft_id, seat_no, cabin, is_exit_row)
SELECT 3, r.n || s.letter, 'ECONOMY', r.n = 4
  FROM generate_series(1, 7) AS r(n)
 CROSS JOIN (VALUES ('A'), ('C'), ('D'), ('F')) AS s(letter);

-- A320neo = 8 business + 48 economy = 56 seats.   ATR = 28 seats.

-- ───────────────────────────────────────────────────────── flight schedule
-- Eight daily rotations across the six routes, for 13 days:
-- three days past (ARRIVED, so the reports have something to aggregate) and
-- ten days forward (SCHEDULED, so there is inventory to sell).

INSERT INTO flight (flight_no, route_id, aircraft_id, sched_dep, sched_arr,
                    base_fare, gate, status)
SELECT t.flight_no,
       t.route_id,
       t.aircraft_id,
       (CURRENT_DATE + d.n) + t.dep,
       (CURRENT_DATE + d.n) + t.dep + (t.dur || ' minutes')::interval,
       t.fare,
       t.gate,
       'SCHEDULED'   -- init_db.py marks past dates ARRIVED once demo
                     -- bookings exist, since a trigger refuses to sell
                     -- a ticket on a flight that has already flown
  FROM (VALUES
        --  no        rt  ac  depart            min   fare    gate
        ('SB101',  1,  1, '06:15'::time,  95,  4250, 'A3'),  -- HYD -> BOM
        ('SB102',  2,  1, '09:05'::time,  95,  4180, 'B1'),  -- BOM -> HYD
        ('SB201',  3,  1, '12:30'::time, 140,  6900, 'A7'),  -- HYD -> DEL
        ('SB202',  4,  1, '16:10'::time, 140,  7150, 'C2'),  -- DEL -> HYD
        ('SB301',  5,  2, '07:30'::time, 130,  6450, 'B4'),  -- BOM -> DEL
        ('SB302',  6,  2, '11:20'::time, 130,  6380, 'C5'),  -- DEL -> BOM
        ('SB103',  1,  3, '18:45'::time,  95,  3900, 'A2'),  -- HYD -> BOM (ATR)
        ('SB104',  2,  3, '21:30'::time,  95,  3850, 'B2')   -- BOM -> HYD (ATR)
       ) AS t(flight_no, route_id, aircraft_id, dep, dur, fare, gate)
 CROSS JOIN generate_series(-3, 9) AS d(n);

COMMIT;

-- ─────────────────────────────────────────────────────────────── summary
SELECT 'airport'  AS relation, count(*) FROM airport
UNION ALL SELECT 'route',    count(*) FROM route
UNION ALL SELECT 'aircraft', count(*) FROM aircraft
UNION ALL SELECT 'seat',     count(*) FROM seat
UNION ALL SELECT 'flight',   count(*) FROM flight;
