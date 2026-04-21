# Traffic Collector

Unified traffic monitoring for:

- Xray-core (3x-ui)
- Amnezia WireGuard

Stores per-user traffic history in PostgreSQL and works with Grafana.

---

## Features

- Multi-source (WG + Xray)
- Automatic detection
- Delta-based stats
- No Prometheus required
- Simple deployment

---

## Quick Start

```bash
git clone https://github.com/yourname/traffic-collector
cd traffic-collector
chmod +x install.sh
./install.sh