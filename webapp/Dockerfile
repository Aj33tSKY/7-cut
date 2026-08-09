# Build context must be the repo root (not webapp/), since the app imports
# ../helpers/*.py directly: `docker build -f webapp/Dockerfile .`
# On Railway: set the Dockerfile Path to `webapp/Dockerfile` and leave the
# build context as the repo root (the default).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --extra webapp --frozen --no-install-project

COPY helpers/ ./helpers/
COPY webapp/ ./webapp/

ENV PATH="/repo/.venv/bin:$PATH"
WORKDIR /repo/webapp

EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips='*': Railway's edge terminates TLS
# and forwards plain HTTP to this container over its internal network, so
# without trusting X-Forwarded-Proto here, request.url_for() in auth.py
# builds http:// URLs and Google rejects the OAuth callback as a
# redirect_uri_mismatch against the https:// one actually registered.
# Safe to trust unconditionally here — Railway's proxy is the only thing
# that can reach this container; it's never directly internet-facing.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
