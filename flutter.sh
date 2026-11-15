#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# Root Flutter wrapper script for HyperPod Analytics.
# Ensures Flutter commands always execute inside the project Nix shell in app/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SCRIPT_DIR}/app"

CMD="cd '${APP_DIR}' && flutter"
if [ $# -gt 0 ]; then
  CMD="${CMD} $(printf '%q ' "$@")"
fi

nix-shell "${SCRIPT_DIR}/shell.nix" --run "${CMD}"
