FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXUS_URL=http://nexus:8081 \
    NEXUS_USER=admin \
    NEXUS_PASS=admin123 \
    REPOSITORY_NAME=maven-snapshots \
    RETAIN_COUNT=3 \
    DRY_RUN=false \
    LOG_LEVEL=INFO \
    SCHEDULE_TIME=03:00 \
    HEALTHCHECK_PORT=8000 \
    DELETE_WORKERS=5

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata curl && \
    ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r appgroup && \
    useradd -r -g appgroup appuser && \
    mkdir -p /var/log /app && \
    touch /var/log/nexus_cleanup.log && \
    chmod 666 /var/log/nexus_cleanup.log && \
    chown -R appuser:appgroup /app /var/log

WORKDIR /app

# Copy and install dependencies first for layer caching
COPY --chown=appuser:appgroup requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appgroup clean_nexus_snapshots.py .

USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["python", "-u", "clean_nexus_snapshots.py"]
