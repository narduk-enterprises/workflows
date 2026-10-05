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

curl -sSfL --retry 3 \
  https://codeload.github.com/rhysd/actionlint/tar.gz/refs/tags/v1.7.12 \
  -o "$task_dir/source.tar.gz"
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
  curl -sSfL --retry 3 "https://go.dev/dl/$go_archive" -o "$task_dir/go.tar.gz"
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
export GOPROXY=https://proxy.golang.org
export GOSUMDB=sum.golang.org
export GOCACHE="$task_dir/go-cache"
export GOMODCACHE="$task_dir/go-modules"
(
  cd "$source_dir"
  "$go_bin" build -mod=readonly -modcacherw -trimpath -o "$task_dir/actionlint" ./cmd/actionlint
)
"$task_dir/actionlint" -version
cp "$task_dir/actionlint" "$output"
