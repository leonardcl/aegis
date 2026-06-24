# Aegis CFO

An AI-agent procurement and audit dashboard for management, powered by the
(mocked) **Hermes Agent**. Built with Flask, SQLAlchemy, Bootstrap 5, and Chart.js.

## Features

- **Dashboard command center** — KPI cards, spend chart, recent ledger activity, urgent procurement.
- **Procurement CRUD** — create / read / update / delete requests, with agent recommendation and vendor scorecards.
- **Approval / guardrail queue** — human-in-the-loop approve / reject with policy reasoning.
- **Audit & ledger** — ledger entries, reconciliation status, compliance replay, exceptions.
- **Floating Hermes Agent chatbot** — available globally via `base.html`, talks to `/agent/chat`.

## Tech stack

Flask · Jinja2 · Bootstrap 5 · SQLAlchemy · SQLite · Chart.js · Gunicorn

## Quickstart

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # optional
python seed.py                    # create + populate the database
python run.py                     # http://localhost:5000
```

## Production

```bash
gunicorn "run:app" --bind 0.0.0.0:8000
```

## Project layout

```
aegis-cfo/
├── app/
│   ├── __init__.py        # app factory
│   ├── config.py
│   ├── extensions.py
│   ├── models.py
│   ├── routes/            # dashboard, procurement, approvals, audit, agent blueprints
│   ├── services/          # procurement, guardrail, audit, ledger, hermes (mocked)
│   ├── templates/
│   └── static/
├── seed.py
├── run.py
└── requirements.txt
```

## Replacing the mocked Hermes Agent

`app/services/hermes_service.py` exposes `ask_hermes_agent(message, context=None)`.
Swap its body for a real Hermes API/CLI call; the rest of the app is unaffected.
