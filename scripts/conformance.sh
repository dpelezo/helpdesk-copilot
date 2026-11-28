#!/usr/bin/env bash
# Run the official MCP conformance suite against every mock IT system (#6).
#
# Each server is served over streamable HTTP from the committed company fixture,
# then checked with the same baseline of failures expected by design. No model
# calls and no network beyond npm. Needs Python deps installed and npx (Node 22).
set -euo pipefail

SUITE="@modelcontextprotocol/conformance@0.1.16"
BASELINE="evals/conformance/baseline.yml"
PORT="${PORT:-8765}"
SERVERS="${*:-identity devices vpn status tickets actions}"

for server in $SERVERS; do
  echo "== $server"
  python -m app.mock.systems "$server" --http --port "$PORT" &
  pid=$!
  trap 'kill "$pid" 2>/dev/null || true' EXIT
  for _ in $(seq 1 120); do
    if (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then break; fi
    sleep 0.5
  done
  npx --yes "$SUITE" server --url "http://127.0.0.1:$PORT/mcp" --expected-failures "$BASELINE"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
done
