# Pitch Studio

Web app for Masters' Union "One Company, One Story" pitches. Admins manage modules, locked facts, recipes, assets, and objections. Users pick five axes and receive a script, branded PPTX deck, and recommended assets.

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
| `OPENROUTER_MODEL` | Defaults to `anthropic/claude-sonnet-4.5` |
| `SECRET_KEY` | Session cookie signing |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Bootstrap admin if the user table is empty |
| `DATABASE_URL` | Defaults to SQLite under `pitch-studio/data/app.db` |
| `SPACES_*` | DigitalOcean Spaces credentials; omit to store files locally |

## DigitalOcean

1. Create an App Platform app from this `Dockerfile` (context: `pitch-studio/`).
2. Set the env vars above as secrets.
3. Create a Spaces bucket if you want durable PPTX storage and set `SPACES_ENDPOINT`, `SPACES_REGION`, `SPACES_KEY`, `SPACES_SECRET`, `SPACES_BUCKET`.
4. Attach a persistent volume mounted at `/app/data` if you stay on SQLite.

```bash
docker build -t pitch-studio .
docker run --rm -p 8080:8080 -e OPENROUTER_API_KEY=sk-or-... pitch-studio
```
