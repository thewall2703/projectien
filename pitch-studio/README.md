# Pitch Studio

Web app for Masters' Union "One Company, One Story" pitches. Admins manage modules, locked facts, recipes, assets, and objections. Users pick five axes and receive a script, a deck, and recommended assets.

## How a deck is built

Slides are not written by the model. Every slide is a real page of the Masters'
Union Brand Deck PDF, so the deck is on brand by construction. The recipe's
module sequence decides which pages are used and the duration axis decides how
many: the cover and closing bookend the deck, and the pages in between are
dealt out round-robin across the modules so each one contributes its strongest
page before any module gets a second.

The page-to-module map lives in `backend/pipeline/brand_deck.py`, with each
module's pages listed best first. Reorder that list to change what a short
deck shows. Pages are rasterised once and cached under
`data/files/brand-deck/pages/`.

The brand deck PDF is resolved from the `Brand Deck` report asset, so run
`python -m backend.sync_assets --types report` before generating. It is too
large for git, and in production it is pulled from Spaces on first use.

## Local development

From the repo root:

```bash
source .venv/bin/activate
pip install -r pitch-studio/requirements.txt
cd pitch-studio
PYTHONPATH=. python -m backend.seed --xlsx "../One Company - One Story (1).xlsx"
PYTHONPATH=. python -m backend.sync_assets --types report
OPENROUTER_API_KEY=sk-or-... PYTHONPATH=. uvicorn backend.main:app --reload --port 8000
```

Media prepare/describe run in a separate worker so the API stays responsive. In another terminal:

```bash
cd pitch-studio
PYTHONPATH=. python -m backend.worker
```

In another terminal:

```bash
cd pitch-studio/frontend
npm install
npm run dev
```

Open http://localhost:5173

Default admin: `admin@example.com` / `changeme`

## Environment

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | Required to generate scripts and decks |
| `OPENROUTER_MODEL` | Defaults to `anthropic/claude-opus-4.6` |
| `OPENROUTER_VERBOSITY` | Claude reasoning effort; defaults to `medium` |
| `SECRET_KEY` | Session cookie signing |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Bootstrap admin if the user table is empty |
| `DATABASE_URL` | Defaults to SQLite under `pitch-studio/data/app.db` |
| `SPACES_*` | DigitalOcean Spaces credentials; omit to store files locally |
| `APIFY_TOKEN` | Required to download YouTube videos at the highest available quality |
| `APIFY_YOUTUBE_QUALITY` | Requested short-side resolution; defaults to `4320` (8K, falls back to next best) |

## DigitalOcean

App Platform spec lives in `.do/app.yaml`. It runs a `web` service and a `media-worker` that polls the `jobs` table for media prepare/describe work. Deploy from a Container Registry image built from this `Dockerfile` (context: `pitch-studio/`). Set `DATABASE_URL`, `SECRET_KEY`, `OPENROUTER_API_KEY`, `APIFY_TOKEN`, and Spaces keys as App Platform secrets on **both** the web and worker components. Attach the existing `mu-pitch-studio-pg` cluster so Trusted Sources allow the app.

```bash
docker build -t pitch-studio .
docker run --rm -p 8080:8080 -e OPENROUTER_API_KEY=sk-or-... pitch-studio
```
