#!/usr/bin/env bash
# Build React UI for deployment to Pi
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND="${SCRIPT_DIR}/../frontend"

cd "$FRONTEND"
if [[ ! -d node_modules ]]; then
    npm install
fi
npm run build
echo "Built → ${FRONTEND}/dist"