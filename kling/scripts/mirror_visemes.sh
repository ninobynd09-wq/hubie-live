#!/bin/sh
# Lip-sync mouth stacks for a MIRRORED desk still (desk_still_v3_mouse_hb: face on the other side of the picture).
# The lip-sync tools' boxes and the talk clips are in the original layout, and the v3 face is an exact mirror of the
# v2 one, so: flip the clip, track and build in the original layout, flip the mouth loops back.
#   kling/scripts/mirror_visemes.sh shots/kling_idle_v3.mp4 assets/cam_visemes_v3            (base: also tracks talks)
#   kling/scripts/mirror_visemes.sh shots/kling_click_v1.mp4 assets/cam_visemes_v3/click idle  (extra clip on that base)
# Sync check runs on the flipped set (closeup.py/check_sync.py crop fixed boxes):
#   LIPSYNC_VISEMES=kling/mirror/vis_<name> tools/lipsync/closeup.py out.mp4
#   LIPSYNC_TRACKS=tools/lipsync/tracks_idle_v3.json LIPSYNC_IDLE=kling/mirror/<name>_flip.mp4 tools/lipsync/check_sync.py out.mp4
set -e
cd "$(dirname "$0")/../.."
SRC=$1; OUT=$2; T=tools/lipsync/tracks_idle_v3.json; BASE=kling/mirror/idle_v3_flip.mp4
ACC="-sws_flags accurate_rnd+full_chroma_int"
name=$(basename "$SRC" .mp4 | sed 's/^kling_//'); FL=kling/mirror/${name}_flip.mp4
mkdir -p kling/mirror "$OUT"
ffmpeg -v error -y -i "$SRC" -vf hflip $ACC -c:v libx264 -preset slow -crf 10 -pix_fmt yuv420p "$FL"
if [ -z "$3" ]; then
  python3 tools/lipsync/track.py "$FL" "$T" "$FL" shots/kling_talk_v1.mp4 shots/kling_talk_v2.mp4
else
  python3 tools/lipsync/track.py "$BASE" kling/mirror/tr_$name.json "$FL"
  python3 -c "import json,sys;d=json.load(open('$T'));d.update(json.load(open('kling/mirror/tr_$name.json')));open('$T','w').write(json.dumps(d))"
fi
LIPSYNC_IDLE="$FL" python3 tools/lipsync/build_visemes.py "$T" kling/mirror/vis_$name
for k in 0 1 2 3 4 5 6 7 8; do
  ffmpeg -v error -y -i kling/mirror/vis_$name/mouth_$k.mp4 -vf hflip $ACC -c:v libx264 -preset slow -crf 12 -pix_fmt yuv420p "$OUT/mouth_$k.mp4"
done
echo "wrote $OUT"
