#!/usr/bin/env bash
# ─────────────────────────────────────────────────────
# Kalshi BTC Perps Framework — entry point
# Run from cron or manually.
# ─────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p data logs

# Install deps if missing (first run only, idempotent)
if [ ! -f ".deps_installed" ]; then
    pip install -q -r requirements.txt 2>/dev/null && touch .deps_installed
fi

MODE="${1:-once}"  # once | loop

case "$MODE" in
    once)
        python main.py "$@" 2>>data/perps.log
        ;;
    loop)
        python main.py --loop "$@" 2>>data/perps.log
        ;;
    *)
        echo "Usage: $0 [once|loop] [--strategy=NAME] [--leverage=X]"
        exit 1
        ;;
esac