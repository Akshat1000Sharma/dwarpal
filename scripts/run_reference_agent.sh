#!/usr/bin/env bash
# Run the published AP2 reference shopping agent against a running Dwarpal.
#
#   ./scripts/run_reference_agent.sh
#   ./scripts/run_reference_agent.sh --base https://your-tunnel.ngrok-free.dev
#   ./scripts/run_reference_agent.sh --prepare-only
#
# Start Dwarpal first: cd backend && uvicorn main:app --port 8000
# The upstream samples are fetched into .reference-agent/, which is gitignored.

set -eu

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
backend="$(dirname "$script_dir")/backend"

python="$backend/.venv/bin/python"
[ -x "$python" ] || python="$backend/.venv/Scripts/python.exe"
[ -x "$python" ] || python="python3"

cd "$backend"
exec "$python" interop/reference_agent/run_upstream_agent.py "$@"
