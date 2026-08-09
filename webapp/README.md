# video-use webapp

The team-facing version of the CLI pipeline: pick a project from a shared
Drive folder, the pipeline runs (transcribe → pack → cut), you review and
adjust the cut in the browser, click Render, the final export lands back in
Drive. See the architecture sketch for the full picture — this file is just
setup.

**Drive layout**: one root folder, shared once with the service account —
permissions inherit to everything underneath, current and future. Under it,
any depth of nesting you want (e.g. `CLIENT/BATCH/VIDEO/`); a "project" is
any folder that directly contains a `raw/` and a `cut/` subfolder
(case-insensitive). The dashboard walks the whole tree looking for those
pairs and shows each one by its full path, e.g. `CLIENT_A / BATCH_A /
VIDEO_A`. Footage + `script.md` go in `raw/`; the finished export lands in
that same project's own `cut/` — there's no single shared output folder.

## What's here

| File | Role |
|---|---|
| `app.py` | FastAPI entrypoint — routes, session middleware, startup wiring |
| `db.py` | SQLite (WAL mode) job store — one `jobs` table |
| `models.py` | `Job` / `JobStatus` |
| `worker.py` | concurrency-bounded pipeline runner (see docstring — this is the important one) |
| `drive.py` | Google Drive client (service account) |
| `auth.py` | Google OAuth login, domain-restricted |
| `routes_projects.py`, `routes_jobs.py` | the API surface |
| `static/dashboard.html` | project list + queue + live status |
| `static/review.html` | the trim/split/reorder UI, job-scoped (ported from `helpers/review_ui.html`) |

Nothing in `helpers/*.py` was touched — the worker calls those scripts
exactly like the CLI pipeline does, just from a job-scoped directory
(`webapp/data/jobs/<job_id>/`) instead of wherever you happened to put a
folder of clips.

## Local dev

```bash
cd video-use
uv sync --extra webapp

cd webapp
GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account.json \
GOOGLE_OAUTH_CLIENT_ID=... \
GOOGLE_OAUTH_CLIENT_SECRET=... \
DRIVE_ROOT_FOLDER_ID=... \
ALLOWED_GOOGLE_DOMAIN=yourcompany.com \
SESSION_SECRET=$(openssl rand -hex 32) \
ELEVENLABS_API_KEY=... \
uv run --project .. --extra webapp uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`.

## One-time Google setup

**Service account (Drive read/write — decoupled from login, see `drive.py`'s
docstring for why):**
1. Google Cloud Console → IAM & Admin → Service Accounts → create one, enable the Drive API on the project.
2. Create a JSON key, keep it out of git. That's `GOOGLE_SERVICE_ACCOUNT_JSON`.
3. Share the single root folder (the one containing all your `CLIENT/BATCH/VIDEO/{raw,cut}` folders) with the service account's email, Editor access. Do this once — it covers every project folder underneath, including ones created later.
4. Grab the root folder's id from its URL (`drive.google.com/drive/folders/<id>`) — that's `DRIVE_ROOT_FOLDER_ID`.

**OAuth client (team login):**
1. Same Cloud project → APIs & Services → Credentials → OAuth client ID → Web application.
2. Authorized redirect URI: `https://<your-deployed-url>/auth/callback` (and `http://127.0.0.1:8000/auth/callback` for local dev).
3. `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` from that client.
4. `ALLOWED_GOOGLE_DOMAIN` restricts login to `@yourcompany.com` accounts.

## Deploying (Railway)

- New service → deploy from this repo.
- Dockerfile path: `webapp/Dockerfile`. Build context stays the repo root (Railway's default) — the image needs both `helpers/` and `webapp/`.
- Attach a volume mounted at `/repo/webapp/data` so `jobs.db` and any in-flight job files survive redeploys.
- Set the env vars listed above, plus `ELEVENLABS_API_KEY`.
- `MAX_CONCURRENT_JOBS` (default `3`) caps how many jobs actively run pipeline stages at once — see `worker.py`. Raise it if the container has CPU to spare; it's a pure config change, not a redeploy-the-architecture change.

## Concurrency, in one paragraph

Many jobs queuing at once — one user queuing several projects, or several
people queuing at the same time — are handled identically: every job goes
through the same `asyncio.Semaphore(MAX_CONCURRENT_JOBS)` in `worker.py`.
A job only holds a slot while it's actually running a pipeline stage; it
releases its slot the moment it reaches "awaiting review" (a human is now
the bottleneck, not CPU) and re-acquires one when Render is clicked. SQLite
in WAL mode handles the job-status writes this produces without any
contention — the real limit on parallelism is the container's CPU, not the
database. This is exercised by a real test (not mocked): 4 jobs queued at
once with the cap set to 2 produce exactly two overlapping pairs, never
more than 2 running at the same instant.
