# SpliceTrail Forensics API - Docker Image
# Multi-stage build for optimized image size

# Build stage
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and install dependencies
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install package with API and perceptual hashing dependencies (for /hash)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[api,hash]"

# Runtime stage
FROM python:3.11-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user
RUN useradd -m -u 1000 splicetrail && \
    mkdir -p /data /app && \
    chown -R splicetrail:splicetrail /data /app

# Set working directory
WORKDIR /app
USER splicetrail

# Copy application code
COPY --chown=splicetrail:splicetrail src/ ./src/

# Environment variables. The index is saved under /data (mount a volume there).
# Set SPLICETRAIL_API_KEY at run time to require an X-API-Key header.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SPLICETRAIL_MAX_TARGETS=1000 \
    SPLICETRAIL_INDEX_DIR=/data

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')"

# Expose API port
EXPOSE 8000

# Volume for persistent data
VOLUME ["/data"]

# Run API server. 0.0.0.0 is needed inside the container; publish the port
# on the host's loopback only (see docker-compose.yml) or set an API key.
CMD ["python", "-m", "splicetrail", "serve", "--host", "0.0.0.0", "--port", "8000"]
