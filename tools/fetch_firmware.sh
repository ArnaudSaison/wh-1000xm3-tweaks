#!/usr/bin/env bash
# Download the WH-1000XM3 firmware straight from Sony's update server and check the SHA-1
# published in Sony's (encrypted) info.xml. Firmware files are Sony's: they are not stored in this repo.
#   usage: tools/fetch_firmware.sh [outdir] [service]   (defaults: ./firmware MDRID291600)
#   service: MDRID291600 = global, MDRID291601 = SPLC, MDRID291602 = CN.
#   Your headset's own service ID is printed by: uv run python -m xm3 --address <addr> survey
#   Needs uv: https://docs.astral.sh/uv/
#   Needs XM3_INFOXML_KEYS (Sony's info.xml AES key, hex): not shipped here, see README.md.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); out=${1:-firmware}; service=${2:-MDRID291600}; mkdir -p "$out"
base=http://info.update.sony.net/HP001/$service
curl -sS -o "$out/info.$service.xml" "$base/info/info.xml"
uv run --no-project --with cryptography python -I "$here/sony_infoxml.py" decrypt "$out/info.$service.xml" HP001 "$service" > "$out/info.$service.dec.xml"
uri=$(grep -o 'ID="FW"[^>]*' "$out/info.$service.dec.xml" | head -1 | grep -o 'URI="[^"]*' | cut -d'"' -f2)
mac=$(grep -o 'ID="FW"[^>]*' "$out/info.$service.dec.xml" | head -1 | grep -o 'MAC="[^"]*' | cut -d'"' -f2)
echo "latest firmware for $service: $uri"
curl -sS -o "$out/$(basename "$uri")" "$uri"
(cd "$out" && echo "$mac  $(basename "$uri")" | sha1sum -c -)
