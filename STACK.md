# Stack

Everything the show uses, as the final Kick run used it (`SHOW_ENV` in `tools/preview.py`, started with
`tools/preview.py start --kick`). Nothing else runs. In that run, 99% of the model calls were Haiku 5.5
(3,552 calls: 2,277 round lines, 1,275 talk-segment lines); Opus 5.5 made 26 chat calls (45 chat messages answered by name; some calls answer several at once, hard moments get fixed lines).

## AI models and services

| What | Name | Where |
|---|---|---|
| Round lines (short and full), talk segments | Claude Haiku 5.5 (`claude-haiku-5-5`) via the Anthropic API | `HUBIE_MODEL_ROUND`, `HUBIE_MODEL_BIT` |
| Chat replies, crisis follow-ups | Claude Opus 5.5 (`claude-opus-5-5`) | `HUBIE_ANTHROPIC_MODEL` (or `HUBIE_MODEL_CHAT`) |
| Busy chat (batched replies, a long queue) | Claude Haiku 5.5 | `HUBIE_MODEL_CHAT_FAST`, else the round model |
| Line writing, offline | `MockProvider` in this repo, no model | `HUBIE_PROVIDER=mock`, used in tests |
| Voice | ElevenLabs text-to-speech, `/with-timestamps` endpoint (`eleven_v4_turbo` by default); the character timings drive the mouth shapes | `HUBIE_TTS=elevenlabs`, `hubie/tts/eleven.py` |
| Voice when ElevenLabs fails | silence as long as the line, subtitles still shown | `SilentTTS` |
| Cam loops (idle desk, click, reactions) | Kling 4.0 image-to-video from the official Hubie art, rendered offline and made loopable | `kling/prompts`, `kling/scripts` |
| Lip-sync | nine mouth-shape copies of the idle loop (mouths cut from Hubie's own Kling frames); the encoder switches between them live (`streamselect` over zmq), picked frame by frame from ElevenLabs' character timings and the audio loudness | `tools/lipsync`, `hubie/audio/lipsync.py` |
| Music bed | 92 public-domain FreePD tracks in four packs, rotated, ducked under the voice | `assets/music/*/LICENSES.md`, `tools/fetch_music.py` |
| Kick | RTMPS ingest for the video; chat read through Kick's documented webhooks (`chat.message.sent`, signature checked) | `KICK_STREAM_URL`, `hubie/chat/kick_webhook.py`, `tools/kick_subscribe.py` |

Flite (ffmpeg `libflite`) and the generic HTTP voice (`HUBIE_TTS=flite` / `http`) are still in the code for tests and
offline runs. They are not used on air.

## Code and libraries

| Library | Use |
|---|---|
| Python 3.10 | everything in `hubie/` |
| `anthropic` Python SDK | Claude calls (`hubie/brain/provider.py`) |
| `httpx` | ElevenLabs requests |
| `playwright` (Python driver only) | drives the game Chrome over its debug port (CDP) |
| `aiohttp` | stage server |
| `pyzmq` | live commands to the encoder's `zmq` filter (mouth, click clip, subtitle plate) |
| `cryptography` | checks Kick's webhook signatures |
| `websockets` | the older, undocumented Kick chat socket (`HUBIE_CHAT=kick`), not used on air |
| `Pillow` | desk layer images (chat list, stats, cards), OCR crops |
| `psutil` | process trees, memory limits |
| `PyYAML` | persona file (`persona/hubie.draft.yaml`) |
| `pytest` | tests |

## System tools

| Tool | Use |
|---|---|
| ffmpeg 4.4 (libx264, AAC, drawtext, `zmq`, `streamselect`, `x11grab`) | the whole picture in one encoder: game capture, cam box, layers, safety strip, HLS or RTMP out |
| Xvfb | the virtual display the game Chrome draws on |
| Google Chrome 150 | the game browser: Housebets house originals at a zero bet, logged-in profile, through a local proxy relay |
| Tesseract OCR | slot bonus-buy checks (slots were off in the final run); tests read the safety strip |
| nginx | TLS for Kick's webhook (forwarded to the loopback port) and the private HLS preview page |
| Inter, Space Grotesk (in `assets/look_mix/fonts`), Poppins, DejaVu Sans | on-screen text |
| systemd | keeps the guardian running (template in `deploy/`) |

## Art

The desk cam is Hubie at his streaming desk: a still of him at the desk, animated with Kling 4.0
into an idle loop, a click clip and reaction clips, plus nine mouth-shape loops built from it
(`assets/cam_visemes_v3`). The video files are not in git (see docs/SETUP.md). `assets/placeholder` holds the old placeholder scene used
by `HUBIE_LAYOUT=classic` and tests.

The screen look of the final run is `HUBIE_LOOK=mix` (`hubie/stage/look_mix.py`): a neon alley plate
(`assets/look_mix`), the cam top left, the chat panel under it, the game top right, his current line on a card and
the stats pills, all composited by the one ffmpeg encoder.
