FROM node:20-slim AS web

WORKDIR /web

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html admin.html vite.config.ts tsconfig.json tsconfig.app.json tsconfig.node.json ./
COPY src/ ./src/
RUN npm run build


FROM python:3.11-slim

WORKDIR /app

ARG CURATIO_GIT_REVISION=unknown
ENV CURATIO_GIT_REVISION=${CURATIO_GIT_REVISION}

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    curl \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY workers/ ./workers/
COPY static/ ./static/
COPY --from=web /web/static/dashboard ./static/dashboard

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Default port (override with PORT env var)
ENV PORT=8000
EXPOSE ${PORT}

# Health check — use curl instead of spawning a Python process (~30MB RAM saved per check)
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -sf http://localhost:${PORT:-8000}/health || exit 1

# Run FastAPI
# No access log: native API paths carry the profile key (/{user_key}/...) and
# /media/ links carry signed tokens, both long-lived bearer secrets.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-access-log"]
