FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

# Install system dependencies (including FFmpeg for video transcoding)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py db.py gpx_parser.py poster_map.py utils.py ./
COPY blueprints/ ./blueprints/
COPY static/ ./static/
COPY templates/ ./templates/

# Copy git metadata if present for versioning (using wildcard to prevent build failure if .git is missing)
COPY .gi[t] ./.git

# Store version and build date during image build
ARG BLAEU_VERSION
RUN if [ -z "$BLAEU_VERSION" ]; then \
        if [ -d .git ]; then \
            apt-get update && apt-get install -y git && \
            git config --global --add safe.directory /app && \
            BLAEU_VERSION=$(git rev-parse --short HEAD) && \
            apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
        else \
            BLAEU_VERSION="unknown"; \
        fi; \
    fi && \
    echo "$BLAEU_VERSION" > version.txt && \
    date -u +"%Y-%m-%dT%H:%M:%SZ" > build_date.txt

# Create a non-privileged user and configure permissions
RUN groupadd -g 10001 blaeu && \
    useradd -u 10001 -g blaeu -d /app -s /sbin/nologin blaeu && \
    mkdir -p /data && \
    chown -R blaeu:blaeu /app /data

USER blaeu

EXPOSE 5000

# Health check using python standard library to avoid external dependencies
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1

# Run with gunicorn and a 120s timeout for heavy video/map rendering
CMD ["sh", "-c", "gunicorn -w ${BLAEU_WORKERS:-2} -t 120 -b 0.0.0.0:5000 app:app"]
