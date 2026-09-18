#!/usr/bin/env bash
# Copy the repository's templates into the package, where the CLI reads them.
#
#   ./scripts/sync_templates.sh    (or: make sync-templates)
#
# The repository copies are canonical -- they are what a person browsing the
# project reads. `tests/test_resources.py` fails when the two drift, and this
# is how you resolve that.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

cp config/config.example.yaml              src/aiworker/data/config.example.yaml
cp config/policy/banned_terms.example.yaml src/aiworker/data/banned_terms.example.yaml
cp .env.example                            src/aiworker/data/env.example
cp docs/05-risk-checklist.md               src/aiworker/data/risk-checklist.md

echo "同梱テンプレートを同期しました:"
ls -1 src/aiworker/data/
