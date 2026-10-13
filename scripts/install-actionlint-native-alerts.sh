#!/usr/bin/env bash
# Compatibility correction for GitHub's native Dependabot read scope.
# Build the checksum-pinned upstream source; change only its permission table.
# Remove this correction when a released actionlint supports read/none natively.
set -euo pipefail

output=${1:?usage: install-actionlint-native-alerts.sh OUTPUT}
source_sha=454800bd4f854592bcfe79b161f71d56e35940eb7016e48a26dd356adc9d400a
task_dir=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/actionlint-native-alerts.XXXXXX")
trap 'rm -rf "$task_dir"' EXIT
output=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$output")

verify_sha() {
  python3 - "$1" "$2" <<'PY'
import hashlib, pathlib, sys
actual = hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()
if actual != sys.argv[2]:
    raise SystemExit(f"checksum mismatch for {pathlib.Path(sys.argv[1]).name}")
PY
}

# Every download is bounded and retried, and a stalled or reset connection
# (curl 56/28, a 5xx) moves on to the next mirror instead of hanging the gate.
# The sha256 check after each fetch is what makes a mirror safe to trust.
fetch() {
  local out=$1 url
  shift
  for url in "$@"; do
    if curl -sSfL --retry 5 --retry-all-errors --retry-delay 5 \
      --connect-timeout 20 --max-time 240 --speed-limit 10240 --speed-time 30 \
      "$url" -o "$out"; then
      return 0
    fi
    echo "fetch failed, trying next source: $url" >&2
  done
  echo "all sources failed for $(basename "$out")" >&2
  return 1
}

fetch "$task_dir/source.tar.gz" \
  https://codeload.github.com/rhysd/actionlint/tar.gz/refs/tags/v1.7.12 \
  https://github.com/rhysd/actionlint/archive/refs/tags/v1.7.12.tar.gz
verify_sha "$task_dir/source.tar.gz" "$source_sha"
tar -xzf "$task_dir/source.tar.gz" -C "$task_dir"
source_dir="$task_dir/actionlint-1.7.12"

# Fixed build toolchain, installed only in this job's disposable directory.
go_bin=$(command -v go || true)
if [ -z "$go_bin" ] || [ "$(GOTOOLCHAIN=local "$go_bin" env GOVERSION)" != go1.26.1 ]; then
  case "$(uname -sm)" in
    'Linux x86_64')
      go_archive=go1.26.1.linux-amd64.tar.gz
      go_sha=031f088e5d955bab8657ede27ad4e3bc5b7c1ba281f05f245bcc304f327c987a
      ;;
    'Darwin arm64')
      go_archive=go1.26.1.darwin-arm64.tar.gz
      go_sha=353df43a7811ce284c8938b5f3c7df40b7bfb6f56cb165b150bc40b5e2dd541f
      ;;
    *) echo 'actionlint compatibility build needs Go 1.26.1' >&2; exit 1 ;;
  esac
  fetch "$task_dir/go.tar.gz" "https://go.dev/dl/$go_archive" "https://dl.google.com/go/$go_archive"
  verify_sha "$task_dir/go.tar.gz" "$go_sha"
  tar -xzf "$task_dir/go.tar.gz" -C "$task_dir"
  go_bin="$task_dir/go/bin/go"
fi

python3 - "$source_dir/rule_permissions.go" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
anchor = '\t"statuses":            {"read", "write", "none"},\n'
if text.count(anchor) != 1 or '"vulnerability-alerts"' in text:
    raise SystemExit('upstream permission table differs; refusing to patch')
path.write_text(text.replace(anchor, anchor + '\t"vulnerability-alerts": {"read", "none"},\n'))
PY

export GOTOOLCHAIN=local
# `|` falls through on any proxy error; go.sum and the checksum database below
# verify every module whichever proxy served it.
export GOPROXY='https://proxy.golang.org|https://goproxy.io|direct'
export GOSUMDB=sum.golang.org
export GOCACHE="$task_dir/go-cache"
export GOMODCACHE="$task_dir/go-modules"
built=0
for attempt in 1 2 3; do
  if (
    cd "$source_dir"
    "$go_bin" build -mod=readonly -modcacherw -trimpath -o "$task_dir/actionlint" ./cmd/actionlint
  ); then
    built=1
    break
  fi
  echo "go build attempt $attempt failed" >&2
  sleep 5
done
[ "$built" = 1 ] || { echo 'actionlint build failed after 3 attempts' >&2; exit 1; }
"$task_dir/actionlint" -version
cp "$task_dir/actionlint" "$output"
