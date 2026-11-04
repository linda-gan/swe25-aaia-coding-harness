#!/usr/bin/env bash
# Kept for compatibility: the URL and commit now live only in config/target.toml.
# Same as: python -m harness fetch cosmicpython [--force]
set -euo pipefail
exec python -m harness fetch cosmicpython "$@"
