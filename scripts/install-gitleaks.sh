#!/bin/sh
set -eu
destination="$1"
version=8.30.1
checksum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
mkdir -p "$destination"
scratch="$(mktemp -d)"
trap 'rm -f "$scratch/tool.tar.gz"; rmdir "$scratch"' EXIT
url="https://github.com/gitleaks/gitleaks/releases/download/v$version"
curl --fail --silent --show-error --location \
  "$url/gitleaks_${version}_linux_x64.tar.gz" > "$scratch/tool.tar.gz"
printf '%s  %s\n' "$checksum" "$scratch/tool.tar.gz" | sha256sum --check
tar --no-same-owner -xzf "$scratch/tool.tar.gz" -C "$destination" gitleaks
chmod 755 "$destination/gitleaks"
"$destination/gitleaks" version
