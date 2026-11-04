# Pharos — a company brain on WhatsApp

Every company already knows the answer to its employees' questions.
It is just buried in old PDFs, spreadsheets and emails, and nobody
remembers which version is true anymore. Pharos is the memory for
that company: one WhatsApp number, no new app, no training. An
employee texts a question, and Pharos answers from the company's
own documents, with the source shown. If it is not in the documents,
it says so instead of guessing.

This repository is the production code for that system, built for
the Meta Global AI Developer Hackathon. It is not a chat demo with
pre-written answers. Every answer in the demo runs through the
same engine, role checks and code gate described below.

## What it does

- **Answers from documents only.** Upload a PDF, DOCX or text file
  on WhatsApp or through `POST /upload` and it is ingested into
  the knowledge graph, then becomes answerable. Answers carry
  their source document and effective date.
- **Remembers what changed.** A correction never overwrites in
  place. The new fact is inserted, the old fact is marked
  superseded, and the link between them is kept. Ask what was true
  before and you get both values, each with its document and date.
- **Flags conflicts honestly.** When a new document disagrees with
  a stored fact, Pharos shows both values, both sources and both
  dates, and informs the approver named in the documents. It does
  not ask the employee to pick the right number, and it never
  silently replaces a fact.
- **Gates answers by role.** Anyone can ask anything. What they
  see depends on their role: CEO sees everything, CFO sees finance
  plus general, CTO sees technical plus general, and employees see
  general only. The gate is enforced in Postgres with row-level
  security, not just in the reply text.
- **Protects sensitive finance.** Sensitive finance questions ask
  for an 8-digit code. Three wrong attempts lock the sender out
  for 15 minutes. A correct code unlocks for 30 minutes, and the
  answer comes from the engine, never from a value written in code.
- **Acts, not just answers.** Send a photo of a receipt captioned
  `receipt` and Pharos emails HR with the vendor, amount and date
  for reimbursement. Gmail delivery is real, through a browser
  login connector — no fake or stub sends, and no credentials in
  `.env` for Gmail.
- **Shows its work.** `GET /graph` and `GET /timeline/<memory-id>`
  expose the stored facts, relations and version history behind
  an answer.

## How it works

Documents are chunked and stored in Postgres with pgvector.
Facts and relations form a knowledge graph with version chains,
so a fact knows what superseded it and what it supersedes.
A question goes through hybrid retrieval — local semantic vectors
plus Postgres full-text plus one-hop graph expansion — with a
recency term and a penalty for superseded rows, then through the
role filter, then to answer synthesis. Weak matches are gated
before they can become an answer.

Meta's models do the language work. Muse Spark through the Meta
Model API (`muse-spark-1.3` via `https://api.meta.ai/v1`) handles
fact extraction and answer synthesis when `META_API_KEY` is set.
Without a key, a deterministic template fallback still answers
from stored facts. Local open models (Meta Llama 3.2, 3.3 and 4
via Ollama) and local EmbeddingGemma vectors mean sensitive
company data can stay on the company's own servers, with no
vendor lock-in and no per-question API cost after setup. Only
fictional sample-company data ever goes through the Meta API.

The surface is WhatsApp only. Free-form replies work within 24
hours of the user's last message; outside that window WhatsApp
requires a pre-approved template, and the webhook says so instead
of faking delivery.

## Run it

Prerequisites: Python 3.12, PostgreSQL 16 with pgvector, and
Ollama for vectors (the app still runs without it, on the keyword
path only).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in your own values; never commit .env
for f in schema.sql schema_engine.sql schema_media.sql schema_actions.sql \
         schema_security.sql schema_conflicts.sql schema_access.sql; do
  psql "$DATABASE_URL" -f "$f"
done
PYTHONPATH=. .venv/bin/python scripts/seed_demo.py   # fictional demo data only
PYTHONPATH=. .venv/bin/uvicorn app.main:app --port 8000
```

Health: `GET /health` returns `{"status": "ok", "vectors": true/false}`.

Set the 8-digit code gate with:

```bash
PYTHONPATH=. .venv/bin/python scripts/set_auth_code.py
```

Connect WhatsApp in your Meta developer app: set the webhook URL
to `https://<your-tunnel>/webhook/whatsapp`, set
`WHATSAPP_VERIFY_TOKEN` to your verify token, and set
`WHATSAPP_TOKEN` and `WHATSAPP_PHONE_ID` in `.env`. Role phones
are comma-separated lists in `WHATSAPP_CEO_PHONE`,
`WHATSAPP_CFO_PHONE` and `WHATSAPP_CTO_PHONE`;
`WHATSAPP_MANAGER_PHONE` still works as a CEO alias.

Gmail is connected from the browser, not from `.env`: open
`GET /auth/gmail` once the server is running and follow the
connect page. Credentials live only in the gitignored
`credentials.json`, tokens only in the gitignored
`data/gmail_token.json`.

## Layout

- `app/main.py` — app wiring, health, routers
- `app/whatsapp.py` + `app/whatsapp_service.py` — WhatsApp webhook and sending
- `app/engine/` — knowledge-graph ingest, relations, dedup, query and routing
- `app/store.py`, `app/retrieve.py`, `app/answer.py` — documents, facts, search, answer shape
- `app/llm.py` — Meta API client, extraction, synthesis, fallback
- `app/embed.py` — local EmbeddingGemma via Ollama
- `app/security.py` — 8-digit code gate, lockout, sensitive-fact checks
- `app/actions.py`, `app/receipts.py`, `app/emailer.py`, `app/gmail_api.py`, `app/gmail_connect.py` — actions and real email delivery
- `app/conflicts.py`, `app/cache.py`, `app/upload.py`, `app/graph.py`, `app/timeline.py`, `app/extract_text.py`, `app/db.py`, `app/config.py` — conflicts, caching, uploads, graph and timeline views, text extraction, database and configuration
- `scripts/seed_demo.py` — fictional Meridian Field Services demo data (21 facts, 8 documents)
- `scripts/set_auth_code.py` — set the 8-digit code gate
- `schema*.sql` — database schema, including row-level security

No code is copied from mem0, Supermemory or EmbeddingGemma
repositories; their patterns are reimplemented here in this
codebase.

## Roadmap, not shipped yet

Next, Pharos will connect productivity apps — Slack, internal
databases, Snowflake and payments — so employees and executives
can ask questions across live company sources in the same chat
and get work done from there, not just get answers. That
connection layer is future work. Nothing in this repository
claims it is already live.

## Honest limits

- The demo uses fictional sample-company data only. Real company
  data should never be sent through the free-credit Meta API.
- The database role running the app must not be a superuser and
  must not have `BYPASSRLS`, or row-level security does not
  actually gate anything. The seed script warns about this.
- WhatsApp delivery depends on a live tunnel and a current
  WhatsApp token; temporary tokens expire after 24 hours.
