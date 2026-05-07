# Multi-stage to keep the runtime image small.
# Stage 1: install deps into a venv that we copy into the final image.
FROM python:3.12-slim AS builder

WORKDIR /app
COPY pyproject.toml ./
COPY server ./server

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir .

# Stage 2: minimal runtime
FROM python:3.12-slim

LABEL org.opencontainers.image.title="copilot-dashboard" \
      org.opencontainers.image.description="Local dashboard for inspecting GitHub Copilot agent sessions" \
      org.opencontainers.image.source="https://github.com/Lemonononon/copilot-dashboard" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CD_HOST=0.0.0.0 \
    CD_PORT=8770

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY server ./server
COPY web ./web

# The dashboard reads VS Code data — mount it read-only at runtime, e.g.
#   docker run -v ~/.config/Code:/data/Code:ro ...
# Override via CD_VSCODE_USER_DIR if your install lives elsewhere.
ENV CD_VSCODE_USER_DIR=/data/Code/User

EXPOSE 8770

# Run as non-root for safety. UID 1000 matches most desktop Linux users so the
# read-only bind mount is accessible without extra chmod.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m -s /bin/bash app
USER app

CMD ["python", "-m", "copilot_dashboard"]
