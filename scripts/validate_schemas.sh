#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

schema_for_example() {
  local base="$1"
  case "$base" in
    candles_backfill.json) echo "schemas/candles.schema.json" ;;
    *) echo "schemas/${base%.json}.schema.json" ;;
  esac
}

echo "==> Validating schemas/examples/valid/*.json"
for example in schemas/examples/valid/*.json; do
  base="$(basename "$example")"
  schema="$(schema_for_example "$base")"
  check-jsonschema --schemafile "$schema" "$example"
done

echo "==> Verifying schemas/examples/invalid/*.json are rejected"
for example in schemas/examples/invalid/*.json; do
  base="$(basename "$example")"
  schema="$(schema_for_example "$base")"
  if check-jsonschema --schemafile "$schema" "$example" >/dev/null 2>&1; then
    echo "ERROR: Invalid example '$example' unexpectedly passed '$schema'!" >&2
    exit 1
  fi
done

echo "==> Validating fixtures/site-data/ top-level contracts"
check-jsonschema --schemafile schemas/manifest.schema.json fixtures/site-data/manifest.json
check-jsonschema --schemafile schemas/whitelist.schema.json fixtures/site-data/whitelist.json
check-jsonschema --schemafile schemas/criteria.schema.json fixtures/site-data/criteria.json
check-jsonschema --schemafile schemas/candidates.schema.json fixtures/site-data/candidates.json

echo "==> Validating fixtures/site-data/ snapshots (JSON + gzipped months)"
snap_files=(fixtures/site-data/snapshots/latest.json fixtures/site-data/snapshots/days/*.json)
for gz in fixtures/site-data/snapshots/months/*.json.gz; do
  out="$TMP_DIR/snap_$(basename "${gz%.gz}")"
  gzip -dc "$gz" > "$out"
  snap_files+=("$out")
done
check-jsonschema --schemafile schemas/snapshot.schema.json "${snap_files[@]}"

echo "==> Validating fixtures/site-data/ fills (JSON + gzipped months)"
fill_files=(fixtures/site-data/fills/*/days/*.json)
idx=0
for gz in fixtures/site-data/fills/*/months/*.json.gz; do
  out="$TMP_DIR/fill_${idx}_$(basename "${gz%.gz}")"
  gzip -dc "$gz" > "$out"
  fill_files+=("$out")
  idx=$((idx + 1))
done
check-jsonschema --schemafile schemas/fills.schema.json "${fill_files[@]}"

echo "==> Validating fixtures/site-data/ candles (gzipped 5m, backfill_15m_1h, overview_4h)"
candle_files=()
idx=0
for gz in \
  fixtures/site-data/candles/*/5m/*.json.gz \
  fixtures/site-data/candles/*/backfill_15m_1h.json.gz \
  fixtures/site-data/candles/*/overview_4h.json.gz; do
  out="$TMP_DIR/candle_${idx}_$(basename "${gz%.gz}")"
  gzip -dc "$gz" > "$out"
  candle_files+=("$out")
  idx=$((idx + 1))
done
check-jsonschema --schemafile schemas/candles.schema.json "${candle_files[@]}"

echo "ALL SCHEMA & SITE-DATA VALIDATIONS PASSED!"
