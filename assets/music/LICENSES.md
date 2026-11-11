# Music bed packs

Each folder is one style pack; its own LICENSES.md lists every track (title, author, length, source URL, license).
All tracks are public domain (CC0) from FreePD.com, via its Internet Archive mirror https://archive.org/details/freepd ,
all instrumental (speech-to-text checked), normalised to -24 LUFS, 48 kHz stereo MP3 128 kb/s.

| Pack | Style | Tracks | Minutes |
|---|---|---|---|
| house/ | Chill / deep house (default) | 20 | 86 |
| lofi/ | Lo-fi beats | 25 | 74 |
| lounge/ | Lounge / jazzy | 23 | 77 |
| night/ | Synthwave / electronic night | 24 | 89 |

Pick one with HUBIE_MUSIC_DIR=assets/music/<pack>, rotate with HUBIE_MUSIC_PACKS="lofi,night" (every
HUBIE_MUSIC_PACK_TRACKS tracks, default 5), or switch mid-stream by writing the pack name into the file named by
HUBIE_MUSIC_PACK_FILE (the preview uses /dev/shm/hubie_stream/music_pack). See hubie/audio/music.py.
