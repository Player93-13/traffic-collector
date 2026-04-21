FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime + build deps required to build some Python packages (psycopg2)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      python3-dev \
      libpq-dev \
      curl \
      ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Upgrade packaging tools to ensure wheels can be built/installed
RUN python -m pip install --upgrade pip setuptools wheel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .

# non-root user
RUN useradd --no-create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9229
ENTRYPOINT ["python", "/app/collector.py"]