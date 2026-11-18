#!/usr/bin/env bash
# Build an upgrade file whose voice prompts and beeps are attenuated (lossless MP3 global_gain edit).
#   usage: tools/build_quiet_prompts.sh original.bin output.bin [steps]   (1 step = 1.5 dB, default 6 = -9 dB)
# UNTESTED ON HARDWARE. See README before flashing anything.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); src=$1; dst=$2; steps=${3:-6}
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
uv run --no-project python -I "$here/appuhdr.py" "$src" "$work" > /dev/null
img=""
for f in "$work"/part03_*; do   # partition 3, sub-image 1 = voice prompts / beeps
  if [ "$(od -An -tx1 -N2 "$f" | tr -d ' ')" = 0001 ]; then img=$f; fi
done
uv run --no-project python -I "$here/csrfs.py" "$img" "$work/fs1" > /dev/null
args=()
while IFS= read -r f; do
  mkdir -p "$work/q/$(dirname "$f")"
  uv run --no-project python -I "$here/mp3gain_lite.py" "$work/fs1/$f" "$work/q/$f" "$steps"
  args+=("1:$f=$work/q/$f")
done < <(cd "$work/fs1" && find . -name '*.mp3' ! -name 'silent_*' | sed 's|^\./||' | sort)
uv run --no-project python -I "$here/repack.py" "$src" "$dst" "${args[@]}"
