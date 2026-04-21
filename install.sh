#!/bin/bash

set -e

echo "=== Traffic Collector Installer ==="

if ! command -v docker &> /dev/null; then
  echo "Docker not found. Installing..."
  curl -fsSL https://get.docker.com | sh
fi

if ! command -v docker compose &> /dev/null; then
  echo "Docker Compose not found. Installing plugin..."
  apt-get update && apt-get install -y docker-compose-plugin
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env file. Please edit it if needed."
fi

echo "Starting collector..."
docker compose up -d --build

echo "Done!"