FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    docker.io \
    wireguard-tools \
    && rm -rf /var/lib/apt/lists/*

COPY collector.py .

RUN pip install psycopg2-binary

CMD ["python", "collector.py"]