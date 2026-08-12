# Image for the web UI. The CLI is the entrypoint, so `docker run … list` and
# the other commands still work; only the default command starts the server.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile alone, so this layer survives every
# change that does not touch pyproject.toml or uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
# --no-editable copies the package into the venv; the default editable install
# would leave it pointing at /app/src, which the runtime stage does not carry.
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm

# /data holds the SQLite database, emails.txt, and CSV exports; in Kubernetes it
# is the mounted volume. /etc/hidemyemail holds the cookie file, mounted from a
# Secret so it can be replaced without rebuilding or restarting.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HIDEMYEMAIL_WEBUI_HOST=0.0.0.0 \
    HIDEMYEMAIL_WEBUI_PORT=8765 \
    HIDEMYEMAIL_COOKIE_FILE=/etc/hidemyemail/cookies.txt \
    HIDEMYEMAIL_DB_FILE=/data/hidemyemail.db \
    HIDEMYEMAIL_OUTPUT_FILE=/data/emails.txt \
    HIDEMYEMAIL_INBOX_CONFIG_FILE=/data/inbox_config.json \
    HIDEMYEMAIL_EXPORT_DIR=/data/exports

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin hidemyemail \
    && mkdir -p /data /etc/hidemyemail \
    && chown -R 1000:1000 /data

COPY --from=build --chown=1000:1000 /app/.venv /app/.venv

USER 1000:1000
WORKDIR /data
EXPOSE 8765

ENTRYPOINT ["hidemyemail"]
CMD ["webui"]
