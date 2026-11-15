#!/usr/bin/env bash
# Guarded wrapper around `swift format`, invoked by the web-ios-swift-*
# pre-commit hooks. iOS formatting is enforced with Xcode on macOS;
# Linux CI may also have Swift, but it is not the iOS toolchain.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  exit 0
fi

if ! command -v swift >/dev/null 2>&1; then
  exit 0
fi

# swift-format 6+ exposes formatting as the `swift format` subcommand. Older
# toolchains may not; treat its absence the same as a missing toolchain.
if ! swift format --version >/dev/null 2>&1; then
  exit 0
fi

exec swift "$@"
