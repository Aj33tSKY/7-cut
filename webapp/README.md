# video-use webapp

The team-facing version of the CLI pipeline: pick a project from a shared
Drive folder, the pipeline runs (transcribe → pack → cut), you review and
adjust the cut in the browser, click Render, the final export lands back in
Drive. See the architecture sketch for the full picture — this file is just
setup.

**Drive layout**: one root folder, shared once with whichever real Google
account authorizes `get_drive_refresh_token.py` (see below) —
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
| `drive.py` | Google Drive client — a real user's OAuth credential, not a service account (see docstring for why) |
| `get_drive_refresh_token.py` | one-time LOCAL script to obtain that credential — not part of the deployed app |
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
GOOGLE_OAUTH_CLIENT_ID=... \
GOOGLE_OAUTH_CLIENT_SECRET=... \
GOOGLE_DRIVE_REFRESH_TOKEN=... \
DRIVE_ROOT_FOLDER_ID=... \
ALLOWED_GOOGLE_DOMAIN=yourcompany.com \
SESSION_SECRET=$(openssl rand -hex 32) \
ELEVENLABS_API_KEY=... \
uv run --project .. --extra webapp uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`.

## One-time Google setup

**OAuth client (used for both team login and Drive access):**
1. Google Cloud Console → APIs & Services → Credentials → OAuth client ID → Web application.
2. Authorized redirect URIs — add both:
   - `https://<your-deployed-url>/auth/callback` (and `http://127.0.0.1:8000/auth/callback` for local dev) — team login.
   - `http://localhost:8765/` — only needed once, for running `get_drive_refresh_token.py` below.
3. `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` from that client.
4. `ALLOWED_GOOGLE_DOMAIN` restricts login to `@yourcompany.com` accounts — leave blank if your team is on personal Gmail accounts rather than Google Workspace (see `auth.py`'s docstring; in that case Google's own consent-screen Test Users list is what actually restricts login).

**Drive access (a real account's OAuth token, not a service account — see
`drive.py`'s docstring for why a service account can't upload here):**
1. Locally, with the same `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` from above:
   ```bash
   GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... \
       uv run --extra webapp python webapp/get_drive_refresh_token.py
   ```
2. A browser opens — log in as whichever Google account should own uploaded files (typically whoever manages the root Drive folder), grant access.
3. The script prints a refresh token — that's `GOOGLE_DRIVE_REFRESH_TOKEN`.
4. Share the single root folder (the one containing all your `CLIENT/BATCH/VIDEO/{raw,cut}` folders) with *that same account* — likely already the case if it's the account that created the folder. Do this once — it covers every project folder underneath, including ones created later.
5. Grab the root folder's id from its URL (`drive.google.com/drive/folders/<id>`) — that's `DRIVE_ROOT_FOLDER_ID`.

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
easily handles the job-status writes this produces at this volume — the
real limit on parallelism is the container's CPU, not the database (and
see `db.py`'s docstring for why this deliberately isn't WAL mode, despite
that being the usual advice for concurrent SQLite access — it requires
memory-mapped files, which are unreliable on network-backed volumes like
Railway's). This is exercised by a real test (not mocked): 4 jobs queued at
once with the cap set to 2 produce exactly two overlapping pairs, never
more than 2 running at the same instant.
