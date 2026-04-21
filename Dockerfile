FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .

# non-root user
RUN useradd --no-create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9229
ENTRYPOINT ["python", "/app/collector.py"]