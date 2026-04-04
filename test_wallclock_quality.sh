#!/bin/bash
# Script para testar qualidade de captura RTSP com e sem wallclock timestamps
# Uso: ./test_wallclock_quality.sh <RTSP_URL>
# Exemplo: ./test_wallclock_quality.sh "rtsp://grn_cam:admin123@192.168.1.20/stream1"

set -e

RTSP_URL="${1:-rtsp://grn_cam:admin123@192.168.1.20/stream1}"
DURATION="${2:-30}"
OUTPUT_DIR="./rtsp_quality_tests"

mkdir -p "$OUTPUT_DIR"

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║ RTSP Quality Test: Wallclock vs. Standard Timestamps         ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""
echo "RTSP URL: $RTSP_URL"
echo "Duration: ${DURATION}s"
echo "Output Directory: $OUTPUT_DIR"
echo ""

# Test 1: Passthrough (copy) SEM wallclock
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Test 1: Passthrough (-c copy) WITHOUT wallclock timestamps"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
OUTPUT1="$OUTPUT_DIR/test1_copy_no_wallclock.mkv"
ffmpeg \
  -rtsp_transport tcp \
  -rtsp_flags prefer_tcp \
  -fflags +genpts \
  -err_detect ignore_err \
  -i "$RTSP_URL" \
  -map 0:v:0 \
  -an \
  -c:v copy \
  -t "$DURATION" \
  -y \
  "$OUTPUT1"

echo ""
echo "✓ Saved: $OUTPUT1"
echo ""

# Test 2: Passthrough (copy) COM wallclock
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Test 2: Passthrough (-c copy) WITH wallclock timestamps"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
OUTPUT2="$OUTPUT_DIR/test2_copy_with_wallclock.mkv"
ffmpeg \
  -rtsp_transport tcp \
  -rtsp_flags prefer_tcp \
  -use_wallclock_as_timestamps 1 \
  -fflags +genpts \
  -err_detect ignore_err \
  -i "$RTSP_URL" \
  -map 0:v:0 \
  -an \
  -c:v copy \
  -t "$DURATION" \
  -y \
  "$OUTPUT2"

echo ""
echo "✓ Saved: $OUTPUT2"
echo ""

# Test 3: Re-encode (libx264) SEM wallclock
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Test 3: Re-encode (-c libx264, veryfast) WITHOUT wallclock"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
OUTPUT3="$OUTPUT_DIR/test3_reencode_no_wallclock.mkv"
ffmpeg \
  -rtsp_transport tcp \
  -rtsp_flags prefer_tcp \
  -fflags +genpts \
  -err_detect ignore_err \
  -i "$RTSP_URL" \
  -map 0:v:0 \
  -an \
  -c:v libx264 \
  -preset veryfast \
  -crf 23 \
  -pix_fmt yuv420p \
  -g 25 \
  -keyint_min 25 \
  -fps_mode vfr \
  -t "$DURATION" \
  -y \
  "$OUTPUT3"

echo ""
echo "✓ Saved: $OUTPUT3"
echo ""

# Test 4: Re-encode (libx264) COM wallclock
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Test 4: Re-encode (-c libx264, veryfast) WITH wallclock"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
OUTPUT4="$OUTPUT_DIR/test4_reencode_with_wallclock.mkv"
ffmpeg \
  -rtsp_transport tcp \
  -rtsp_flags prefer_tcp \
  -use_wallclock_as_timestamps 1 \
  -fflags +genpts \
  -err_detect ignore_err \
  -i "$RTSP_URL" \
  -map 0:v:0 \
  -an \
  -c:v libx264 \
  -preset veryfast \
  -crf 23 \
  -pix_fmt yuv420p \
  -g 25 \
  -keyint_min 25 \
  -fps_mode vfr \
  -t "$DURATION" \
  -y \
  "$OUTPUT4"

echo ""
echo "✓ Saved: $OUTPUT4"
echo ""

# Summary
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║ Test Complete                                                ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""
echo "Outputs saved to: $OUTPUT_DIR"
echo ""
echo "Files:"
ls -lh "$OUTPUT_DIR"/*.mkv
echo ""
echo "Next steps:"
echo "  1. Download the .mkv files to your local machine"
echo "  2. Compare visual quality (playback smoothness, frame corruption)"
echo "  3. Check for jitter or stuttering in each test"
echo ""
echo "Recommendations for testing:"
echo "  • Play each file in VLC: Media → Open → (select file)"
echo "  • Look for: stutter, frame drops, visual artifacts, sync drift"
echo "  • Compare same-position (e.g., 15s mark) across all tests"
echo ""
