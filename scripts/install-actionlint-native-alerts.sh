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
    raise SystemExit(f"checksum mismatch for {pathlib.Path(sys.argv[1]).name}: expected {sys.argv[2]}, got {actual}")
PY
}

# Every download is bounded and retried, and a stalled or reset connection
# (curl 56/28, a 5xx) moves on to the next mirror instead of hanging the gate.
# The sha256 check after each fetch is what makes a mirror safe to trust.
fetch() {
  local out=$1 url
  shift
  for url in "$@"; do
    if curl -sS --fail --location --retry 5 --retry-all-errors --retry-delay 2 \
      --connect-timeout 20 --max-time 300 --speed-limit 10240 --speed-time 45 \
      "$url" -o "$out"; then
      return 0
    fi
    echo "fetch failed, trying next source: $url" >&2
  done
  echo "all sources failed for $(basename "$out")" >&2
  return 1
}

# Fast path: the output of the build below, published once as a release asset
# of this repo and pinned by sha256. It is ~8 MB from github.com, the host the
# job already depends on to check out, instead of a ~75 MB Go toolchain from
# go.dev, which stalled the required gate (workflows#138). Any fetch or checksum
# failure falls through to the checksum-pinned source build.
# Rebuild: run this script, then publish the output as a new release asset and
# move the two values below (and the callers' installer pin) together.
prebuilt_base=https://github.com/narduk-enterprises/workflows/releases/download/actionlint-1.7.12-native-alerts-1
case "$(uname -sm)" in
  'Linux x86_64')
    prebuilt_asset=actionlint-linux-amd64
    prebuilt_sha=ff9b4c375dac15c384bb745804beaac248df0d9f7db88e1f20cc2c3649584d17
    ;;
  *) prebuilt_asset= ;;
esac
# Cache: a binary kept from an earlier run (actions/cache restores this dir,
# keyed by version+OS+arch) is reused only if it still matches the pinned
# sha256, so a stale or tampered cache falls through to the download. A
# different actionlint on PATH is not reused: it lacks the native-alerts patch.
cache_dir=${ACTIONLINT_CACHE_DIR:-${RUNNER_TOOL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}}/actionlint-native-alerts/1.7.12-$(uname -s)-$(uname -m)}
if [ -n "$prebuilt_asset" ] && [ -f "$cache_dir/actionlint" ] \
  && verify_sha "$cache_dir/actionlint" "$prebuilt_sha" \
  && chmod +x "$cache_dir/actionlint" \
  && "$cache_dir/actionlint" -version >/dev/null 2>&1; then
  echo "using cached actionlint from $cache_dir" >&2
  "$cache_dir/actionlint" -version
  cp "$cache_dir/actionlint" "$output"
  exit 0
fi
if [ -n "$prebuilt_asset" ] \
  && fetch "$task_dir/prebuilt" "$prebuilt_base/$prebuilt_asset" \
  && verify_sha "$task_dir/prebuilt" "$prebuilt_sha" \
  && chmod +x "$task_dir/prebuilt" \
  && "$task_dir/prebuilt" -version >/dev/null; then
  "$task_dir/prebuilt" -version
  cp "$task_dir/prebuilt" "$output"
  mkdir -p "$cache_dir" && cp "$task_dir/prebuilt" "$cache_dir/actionlint" || true
  exit 0
fi
echo "prebuilt actionlint unavailable; building from the pinned source" >&2

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
