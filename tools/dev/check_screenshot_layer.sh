#!/bin/bash
# Checks VK_LAYER_MESA_screenshot without the game: runs vkcube (a small window for a few seconds) with the
# layer enabled on a private control socket, grabs one frame through bridge/src/bg3ai/screen.py, and checks the
# PNG. Uses the same PRIME offload as runner/launch_game.sh (BG3AI_PRIME=auto|1|0), so on a hybrid laptop it
# tests the layer on top of the NVIDIA driver.
#
#   tools/dev/check_screenshot_layer.sh [--wsi xcb|wayland]     (default xcb: SDL2 games use X11/XWayland)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
wsi=xcb
[ "${1:-}" = --wsi ] && wsi="$2"
command -v vkcube >/dev/null || { echo "no vkcube (apt install vulkan-tools)" >&2; exit 1; }

out="$(mktemp -d)"
trap 'kill "$pid" 2>/dev/null || true; rm -rf "$out"' EXIT
sock="bg3ai-shot-check-$$"
prime_env=()
case "${BG3AI_PRIME:-auto}" in
    1) prime_env=(__NV_PRIME_RENDER_OFFLOAD=1 __VK_LAYER_NV_optimus=NVIDIA_only __GLX_VENDOR_LIBRARY_NAME=nvidia) ;;
    0) ;;
    *) if grep -qs 0x10de /sys/bus/pci/devices/*/vendor && [ -e /proc/driver/nvidia/version ]; then
           prime_env=(__NV_PRIME_RENDER_OFFLOAD=1 __VK_LAYER_NV_optimus=NVIDIA_only __GLX_VENDOR_LIBRARY_NAME=nvidia)
       fi ;;
esac

env "${prime_env[@]}" VK_LOADER_LAYERS_ENABLE=VK_LAYER_MESA_screenshot \
    "VK_LAYER_MESA_SCREENSHOT_CONFIG=comms;control=$sock;output_dir=$out;log_type=info" \
    timeout 20 vkcube --wsi "$wsi" --c 1200 --width 320 --height 240 >"$out/vkcube.log" 2>&1 &
pid=$!
sleep 2

rc=0
PYTHONPATH="$REPO/bridge/src" python3 -I - "$out" "$sock" <<'EOF' || rc=$?
import sys, time
from pathlib import Path
sys.path.insert(0, __import__("os").environ["PYTHONPATH"])
from bg3ai.screen import ScreenCapture
out, sock = Path(sys.argv[1]), sys.argv[2]
t = time.monotonic()
png = ScreenCapture(out, sock).capture_png()
data = png.read_bytes()
assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
print(f"captured {w}x{h} PNG, {len(data)} bytes, in {time.monotonic() - t:.3f} s")
assert (w, h) == (320, 240), "unexpected frame size"
EOF
kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true
grep -m1 'Selected GPU' "$out/vkcube.log" || true
grep -i 'mesa-screenshot' "$out/vkcube.log" | grep -iv 'renamed\|waiting' | head -3 || true
if [ "$rc" = 0 ]; then echo "OK: screenshot layer works"; else echo "FAIL (vkcube log:)"; cat "$out/vkcube.log"; fi
exit "$rc"
