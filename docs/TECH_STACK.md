# SkyBase — Tech Stack, Software & Credentials

**DBMS Project-Based Learning · Project 18 — Airline Reservation & Flight Operations**

Everything this project runs on, what each piece is for, and every default
credential in one place. Written for the report and the viva.

---

## 1 · The stack in one line

> **Python 3.13 + Flask** serving server-rendered **Jinja** templates, talking
> **hand-written SQL** to **PostgreSQL**, deployed on **Render** with the
> database on **Neon**.

No ORM, no JavaScript framework, no build step. That is deliberate: this is a
DBMS assignment, and an ORM would hide the exact thing being assessed.

---

## 2 · Languages

| Language | Version | Used for |
|---|---|---|
| **Python** | 3.13.5 | Application logic — routes, transactions, business rules |
| **SQL** (PostgreSQL dialect) | — | **The core of the project.** Schema, constraints, triggers, views, and every query. Hand-written throughout |
| **HTML** (Jinja2 templates) | — | All 14 pages, rendered on the server |
| **CSS** | — | ~900 lines, hand-written. No Tailwind/Bootstrap |
| **JavaScript** | ES2022, vanilla | ~380 lines. Seat picker, split-flap board, animated backdrop, concurrency lab. No framework |

---

## 3 · Python packages

Everything in `requirements.txt`, and why each one is there.

| Package | Version | Why it is here |
|---|---|---|
| **Flask** | 3.1.0 | The web framework — routing, sessions, request handling |
| **psycopg** (+ `psycopg-binary`) | 3.2.3 | PostgreSQL driver. Version 3, the current generation |
| **psycopg-pool** | 3.2.4 | Connection pooling. Without it every page paid for a fresh TLS handshake to the database (1.57s → 0.12s per page) |
| **gunicorn** | 23.0.0 | Production WSGI server on Render. Flask's built-in server is development-only |
| **python-dotenv** | 1.0.1 | Loads `.env.local` so credentials stay out of the source code |

Pulled in automatically by Flask (not chosen directly): `Werkzeug` 3.1.9 (also
supplies our **password hashing**), `Jinja2` 3.1.6 (template engine),
`MarkupSafe`, `click`, `itsdangerous` (signs the session cookie), `blinker`.

---

## 4 · Database

| | |
|---|---|
| **Engine** | PostgreSQL |
| **Local (development)** | 17.11, installed via Homebrew |
| **Cloud (production)** | 18.6, hosted on **Neon** |
| **Driver** | psycopg 3 |
| **Connection style** | Pooled, via Neon's PgBouncer endpoint |

### Why PostgreSQL and not MySQL

MySQL 8.0.46 *is* installed on this machine, and the project statement allows
either. PostgreSQL was chosen for one specific reason:

```sql
CREATE UNIQUE INDEX uq_ticket_seat_per_flight
    ON ticket (flight_id, seat_id)
 WHERE seat_id IS NOT NULL;      -- <- MySQL cannot do this
```

That `WHERE` makes it a **partial index**. It is what lets a cancelled ticket
free up seat 14A for the next passenger while two *live* tickets still cannot
share it. MySQL has no partial indexes, so the same rule would have needed a
trigger — strictly worse, because a trigger can be bypassed in ways a unique
index cannot.

PostgreSQL features used that MySQL lacks or does differently: partial unique
indexes, `FILTER` on aggregates, `GENERATED ALWAYS AS ... STORED` on a date
derived from a timestamp, `string_agg`, regex `~` in `CHECK` constraints, and
`plpgsql` trigger functions.

### What is in the database

| | Count |
|---|---|
| Relations (tables) | **9**, all in 3NF / BCNF |
| Declared constraints | **55** — PK, FK, UNIQUE, CHECK |
| Triggers | **3** — rules no `CHECK` can express |
| Views | **5** — the reports |
| Partial unique index | **1** — the seat guarantee |

---

## 5 · Hosting & services

| Service | What it runs | Plan | Region |
|---|---|---|---|
| **Render** | The Flask web app, under gunicorn | Free | Singapore |
| **Neon** | The PostgreSQL database | Free | ap-southeast-1 (Singapore) |
| **GitHub** | Source code, auto-deploys to Render on push | Public repo | — |
| **Google Fonts** | 4 typefaces, loaded by the browser | Free | — |

**Live site:** https://skybase-8tjo.onrender.com/
**Repository:** https://github.com/joedan07/skybase

> **Note on Render's free tier:** the service sleeps after 15 minutes idle, so
> the first request after a quiet spell takes ~40 seconds to wake. Open the
> site a minute before any demo, or set up a free pinger at cron-job.org
> hitting the URL every 10 minutes.

---

## 6 · Development tools

| Tool | Version | Used for |
|---|---|---|
| **Homebrew** | — | Installing PostgreSQL 17 locally |
| **Git / GitHub CLI** | — | Version control; `gh` created the repo |
| **Python venv** | — | Isolated dependencies in `.venv/` |
| **Google Chrome (headless)** | — | Rendering the ER diagram SVG to PNG |
| **MySQL Workbench / MySQL 8.0.46** | 8.0.46 | **Installed on the machine but not used by this project.** Listed here only so it is not mistaken for a dependency |
| **Node.js** | 22.19.0 | **Not used at runtime.** Only for `node --check` syntax-checking the JavaScript |

---

## 7 · Typefaces

All four from Google Fonts, loaded in `templates/base.html`:

| Font | Role |
|---|---|
| **Archivo** | Display — headings, IATA codes, big numbers |
| **Inter** | Body text |
| **Instrument Serif** | The one italic editorial line per page |
| **JetBrains Mono** | The "system voice" — flight numbers, times, SQL, SQLSTATEs |

---

## 8 · ALL CREDENTIALS

> The customer logins below are deliberately public so anyone can try the
> site. **The live admin password and the Neon database password are not in
> this file** — this repository is public, and either one would hand over the
> running system. Both are in `private/TECH_STACK_WITH_SECRETS.md`, which is
> outside the repository, for the project report.

### 8.1 · Live site — https://skybase-8tjo.onrender.com/

Sign in at **`/login`**. Admins are redirected to `/admin` automatically.

| Role | Email | Password |
|---|---|---|
| **Admin / Operations** | `ops@skybase.in` | *(not published — see your private notes)* |
| Customer | `asha@example.com` | `flyskybase` |
| Customer | `vikram@example.com` | `flyskybase` |
| Customer | `neha@example.com` | `flyskybase` |
| Customer | `imran@example.com` | `flyskybase` |
| Customer | `divya@example.com` | `flyskybase` |
| Customer | `rahul@example.com` | `flyskybase` |

The admin password was generated at random when the live database was seeded.
It is **not** in the repository — `init_db.py` refuses to seed a remote
database with a default password, precisely because the repo is public.

### 8.2 · Local development

| Role | Email | Password |
|---|---|---|
| **Admin / Operations** | `ops@skybase.in` | `skybase-ops` |
| Customer | `asha@example.com` (and the five above) | `flyskybase` |

Defined in `init_db.py` as `ADMIN` and `CUSTOMER_PASSWORD`.

### 8.3 · Databases

| | Value |
|---|---|
| **Local database name** | `skybase` |
| **Local user** | your macOS username (`joedaniel`), no password — Postgres peer auth |
| **Local connection string** | `postgresql:///skybase` |
| **Neon database name** | `neondb` |
| **Neon user** | `neondb_owner` |
| **Neon password** | *(not published — Render env var + your private notes)* |
| **Neon host** | `ep-dawn-boat-azlgce2a-pooler.c-3.ap-southeast-1.aws.neon.tech` |

The full connection string lives in exactly two places: the `DATABASE_URL`
environment variable in the Render dashboard, and `.env.neon` on the
development machine (which git ignores). It is **deliberately not written
down here**, because this file is in a public repository — the string grants
full read and write access to the live database, including `DROP TABLE`.

```
postgresql://neondb_owner:<password>@ep-dawn-boat-azlgce2a-pooler.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require
```

### 8.4 · MySQL (installed on the machine, unused by this project)

| | Value |
|---|---|
| Server | MySQL 8.0.46 at `/usr/local/mysql` |
| Root user | `root` — password set by you, not stored anywhere in this project |
| `skybase` user | Created early on but **never used**; the project moved to PostgreSQL |

### 8.5 · Environment variables

| Variable | Where it is set | Purpose |
|---|---|---|
| `DATABASE_URL` | Render dashboard · `.env.local` locally | The connection string |
| `SESSION_SECRET` | Render generates it | Signs the login cookie |
| `ADMIN_PASSWORD` | Set only when running `init_db.py` | The admin account password at seed time |
| `PORT` | Render sets it | Which port gunicorn binds |
| `DB_POOL_MAX` | Optional, defaults to 6 | Connection pool ceiling |

Local secrets live in `.env.local` and `.env.neon`. **Neither is in git** —
`.gitignore` ignores `.env*` with an exception only for `.env.example`.

> **How passwords are stored:** never in plain text. `werkzeug.security`
> hashes them with **scrypt** before they reach the `passenger.password_hash`
> column, and sign-in compares hashes. Even with full database access, the
> passwords cannot be read back.

---

## 9 · How to run it

### Locally

```bash
cd skybase
.venv/bin/python app.py          # http://127.0.0.1:5001
```

Rebuild the database from nothing:

```bash
.venv/bin/python init_db.py      # schema + seed + accounts + demo bookings
```

### On Render

Pushing to `main` on GitHub redeploys automatically. `render.yaml` holds the
build and start commands:

```yaml
buildCommand: pip install -r requirements.txt
startCommand: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4
```

---

## 10 · Project layout

```
app.py              Flask routes — raw SQL, no ORM
db.py               Connection pool + the booking / cancel / check-in transactions
init_db.py          One-command build: schema, seed, accounts, demo traffic
sql/schema.sql      The DDL — PK, FK, UNIQUE, CHECK, triggers, views
sql/seed.sql        Fixed reference data + the flight schedule
templates/          14 Jinja pages (customer + admin/)
static/css/app.css  The design system, hand-written
static/js/app.js    Seat picker, split-flap board, form logic
static/js/atmos.js  The animated backdrop
render.yaml         Render deployment blueprint
docs/               Review 1 deliverables, ER diagram, this file
```
