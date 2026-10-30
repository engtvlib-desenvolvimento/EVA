#!/usr/bin/env bash
set -euo pipefail
# Material sintético: CLAQUETE 1s → BLACK 5s → CONTEÚDO 5s → BLACK 5s
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i 'color=c=gray:s=640x360:r=25:d=1' \
  -f lavfi -i 'color=c=black:s=640x360:r=25:d=5' \
  -f lavfi -i 'testsrc2=s=640x360:r=25:d=5' \
  -f lavfi -i 'color=c=black:s=640x360:r=25:d=5' \
  -f lavfi -i 'sine=frequency=440:sample_rate=48000:duration=16' \
  -filter_complex '[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v];[4:a]pan=stereo|c0=c0|c1=c0[a]' \
  -map '[v]' -map '[a]' -c:v mpeg2video -b:v 8M -pix_fmt yuv422p \
  -c:a pcm_s24le -f mxf "$(dirname "$0")/DEMO.mxf"
