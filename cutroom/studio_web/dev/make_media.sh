#!/usr/bin/env bash
# Generate synthetic media for the Cutroom Studio mock server (dev only).
# Usage: make_media.sh [MEDIA_DIR]   — needs ffmpeg. Runs at most 2 ffmpeg at once.
set -euo pipefail
OUT="${1:-${TMPDIR:-/tmp}/cutroom_mock_media}"
mkdir -p "$OUT"/proxy "$OUT"/thumbs "$OUT"/sheets "$OUT"/songs "$OUT"/render "$OUT"/ref "$OUT"/grabs
FF="ffmpeg -hide_banner -loglevel error -y"
SRCS=("testsrc2=s=640x360:r=25" "smptehdbars=s=640x360:r=25" "mandelbrot=s=640x360:r=25" \
      "life=s=640x360:r=25:mold=10:ratio=0.3:death_color=#1a2a3a:life_color=#e0c070" \
      "cellauto=s=640x360:r=25:rule=110" "rgbtestsrc=s=640x360:r=25")
DURS=(34 30 38 32 36 40)
FREQS=(220 330 440 262 392 523)

proxy() {
  local i=$1 f="$OUT/proxy/p$1.mp4"
  [ -s "$f" ] && return 0
  local hue="hue=h=t*12"
  $FF -f lavfi -i "${SRCS[$i]},format=yuv420p,$hue" -f lavfi -i "sine=f=${FREQS[$i]}:sample_rate=48000" \
      -t "${DURS[$i]}" -c:v libx264 -preset veryfast -crf 28 -g 25 -pix_fmt yuv420p \
      -c:a aac -b:a 96k -shortest -movflags +faststart "$f"
}
thumbs() {
  local i=$1 d="$OUT/thumbs/$1"
  mkdir -p "$d"
  [ -s "$d/0.jpg" ] || $FF -i "$OUT/proxy/p$i.mp4" -vf "fps=1,scale=320:-2" -q:v 5 -start_number 0 "$d/%d.jpg"
  [ -s "$OUT/sheets/p$i.jpg" ] || $FF -i "$OUT/proxy/p$i.mp4" -vf "fps=12/${DURS[$i]},scale=320:-2,tile=4x3" -frames:v 1 -q:v 5 "$OUT/sheets/p$i.jpg"
}
for i in 0 1 2 3 4 5; do
  ( proxy $i && thumbs $i ) &
  if (( (i+1) % 2 == 0 )); then wait; fi
done
wait

song() {
  local name=$1 bpm=$2 freq=$3 f="$OUT/songs/$1.m4a"
  [ -s "$f" ] && return 0
  local per; per=$(python3 -c "print(60/$bpm)")
  $FF -f lavfi -i "sine=f=$freq:sample_rate=48000:duration=150" \
      -f lavfi -i "aevalsrc='0.6*sin(2*PI*1800*t)*exp(-60*mod(t\,$per))':s=48000:d=150" \
      -filter_complex "[0]volume=0.25[a];[1]volume=0.8[b];[a][b]amix=inputs=2:normalize=0" \
      -c:a aac -b:a 128k "$f"
}
song s_drive 96 196 & song s_summer 120 247 & wait
song s_night 128 165

if [ ! -s "$OUT/render/daytrip_v2_preview.mp4" ]; then
  $FF -f lavfi -i "testsrc2=s=1280x720:r=25,format=yuv420p" -f lavfi -i "sine=f=300:sample_rate=48000" \
      -t 60 -c:v libx264 -preset veryfast -crf 28 -pix_fmt yuv420p -c:a aac -b:a 128k -shortest \
      -movflags +faststart "$OUT/render/daytrip_v2_preview.mp4" &
fi
if [ ! -s "$OUT/ref/ref_edit.mp4" ]; then
  $FF -f lavfi -i "smptebars=s=1280x720:r=25,format=yuv420p,hue=h=t*20" -f lavfi -i "sine=f=180:sample_rate=48000" \
      -t 45 -c:v libx264 -preset veryfast -crf 28 -pix_fmt yuv420p -c:a aac -b:a 128k -shortest \
      -movflags +faststart "$OUT/ref/ref_edit.mp4" &
fi
wait
echo "media ready in $OUT"
