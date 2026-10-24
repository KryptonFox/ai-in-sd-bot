#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
    echo 'Нужен uv. См. README.md.' >&2
    exit 1
fi
exec uv run --project "$script_dir/.." --locked --no-env-file python "$script_dir/bootstrap.py" "$@"
