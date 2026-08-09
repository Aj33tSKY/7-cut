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
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
