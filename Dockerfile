# syntax=docker/dockerfile:1

# --- Base image ------------------------------------------------------------
# Python 3.12 matches the project's development / CI interpreter and satisfies
# every pin in requirements.txt (Streamlit 1.38, LangChain 1.3, LangGraph 1.2,
# pydantic 2.9, PyMuPDF 1.24 — all ship manylinux wheels for CPython 3.12, so
# no compiler / build tooling is needed in the image).
#
# Digest pinning: a specific image digest is deliberately NOT hard-coded here.
# A digest must be verified against the live registry, which was not possible
# in the environment this file was authored in; hard-coding an unverified
# digest would make `docker build` non-reproducible or simply fail. The tag
# below pins the Debian release ("bookworm") for practical reproducibility. To
# pin a digest in your own environment:
#     docker pull python:3.12-slim-bookworm
#     docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim-bookworm
# then replace the tag below with  python:3.12-slim-bookworm@sha256:<digest>
FROM python:3.12-slim-bookworm

# --- Runtime environment -------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# --- Dependencies (own layer; cached unless requirements.txt changes) ----
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

# --- Application --------------------------------------------------------
# .dockerignore keeps .env, venv/, outputs/, uploads/, logs/, caches and VCS
# metadata out of the build context and therefore out of the image.
COPY . .

# --- Non-root user + writable runtime directories -----------------------
# outputs/ uploads/ logs/ hold the JSON / file persistence written at runtime.
# They are created and chowned so the container also works with no volume
# mounted; mount volumes over them in production to persist data across
# container restarts.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/outputs /app/uploads /app/logs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8501

# --- Healthcheck -----------------------------------------------------
# Streamlit's built-in readiness endpoint. curl / wget are absent from the
# slim image, so probe with the interpreter that is guaranteed to be present.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4).status == 200 else 1)"

# --- Entrypoint ------------------------------------------------------
# Bind to all interfaces on 8501 so the port is reachable from outside the
# container. .streamlit/config.toml (tracked) already sets headless=true,
# maxUploadSize=25 and showErrorDetails=false.
CMD ["streamlit", "run", "app/ui/streamlit_app.py", \
     "--server.address=0.0.0.0", "--server.port=8501"]
