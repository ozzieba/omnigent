#!/usr/bin/env bash
set -euo pipefail

uv_tools="$RUNNER_TEMP/uv-tools"
uv_site="$uv_tools/site"
uv_requirements="$RUNNER_TEMP/uv-requirements.txt"
mkdir -p "$uv_site"
printf '%s\n' \
  'uv==0.12.9 --hash=sha256:5badfd805fd88bf99b4b4f044f6e8f762f1892cab27477f4427bb473e93dd049' \
  > "$uv_requirements"
python3 -m pip install \
  --target "$uv_site" \
  --index-url https://pypi.org/simple \
  --no-deps \
  --only-binary=:all: \
  --require-hashes \
  --disable-pip-version-check \
  -r "$uv_requirements"
cat > "$uv_tools/uv" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$script_dir/site${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m uv "$@"
LAUNCHER
chmod +x "$uv_tools/uv"
echo "$uv_tools" >> "$GITHUB_PATH"
"$uv_tools/uv" --version
