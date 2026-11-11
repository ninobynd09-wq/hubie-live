# Setup

Hubie runs as one Python process tree on one CPU-only Linux box: a game Chrome on a virtual display plays Housebets
house originals at a zero bet, Claude writes the lines, ElevenLabs speaks them, and one ffmpeg encoder draws the
scene and streams it to Kick. Kick chat comes in through webhooks. Models and tools: [STACK.md](../STACK.md).
How each part works: [README](../README.md).

## 1. What you need

- **Machine:** Linux (built on Ubuntu 22.04), CPU only. The full show uses 1 to 2 cores and about 2 GB of RAM.
- **Packages:** `ffmpeg` 4.4 with libx264, drawtext and zmq, `xvfb`, `google-chrome-stable`, `tesseract-ocr`,
  `fonts-dejavu`, Poppins Bold / ExtraBold in `~/.fonts/`, Python 3.10 with `venv`, and `nginx` (TLS for the Kick
  webhook, optional preview page).
- **Accounts and keys:**
  - Anthropic API key (Claude).
  - ElevenLabs API key and a voice id.
  - Kick: the channel's stream URL and key, and a developer app (client id and secret, `events:subscribe` scope).
  - A Housebets account, logged in once in the game Chrome profile (`HUBIE_HB_PROFILE`).
  - An HTTP proxy for the game Chrome (`HUBIE_GAMES_PROXY`, used through the local relay in `tools/housebets/relay.py`).

Why the proxy: housebets.com answers 403 to server/datacenter IPs, so the game browser on the VPS goes through an HTTP proxy.
If the site shows its "verify you are human" page, the adapter clicks the checkbox and waits for the game to load.

## 2. Install

```bash
git clone https://github.com/ninobynd09-wq/hubie-live hubie_stream && cd hubie_stream
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -m "not chaos" -q     # under a minute, no network, no browser
python3 tools/fetch_music.py                     # optional: the public-domain music bed (about 300 MB)
cp .env.example .env                             # then fill in the keys (step 3)
```

The cam clips (idle desk loop, nine mouth-shape loops, a click clip, four reactions) are video files and are not in
the repo. Render them with Kling image-to-video from `kling/prompts` (`kling/scripts/submit.py`, `poll.py`), make
them loop and cut the mouth shapes with `kling/scripts/*` and `tools/lipsync/build_visemes.py`, then point
`HUBIE_CAM_IDLE`, `HUBIE_MOUTH_DIR`, `HUBIE_CLICK_MOUTH_DIR` and `HUBIE_REACT_*` at them. The desk scene needs
at least the idle loop; `HUBIE_LAYOUT=classic` runs on the placeholder art in `assets/placeholder` without any.

## 3. Configure

Every key is listed in `.env.example` (no real values in it). The ones the live show needs:

| Key | What |
|---|---|
| `ANTHROPIC_API_KEY` | Claude |
| `ELEVENLABS_API_KEY`, `HUBIE_ELEVEN_VOICE` | Hubie's voice |
| `KICK_STREAM_URL`, `KICK_STREAM_KEY` | where the video goes (`rtmps://...`) |
| `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET`, `KICK_BROADCASTER_USER_ID` | Kick app, for `tools/kick_subscribe.py` |
| `HUBIE_GAMES_PROXY` | upstream proxy for the game Chrome, `http://user:pass@host:port` |
| `HUBIE_PREVIEW_DOMAIN`, `HUBIE_PREVIEW_USER` | optional: host name and basic-auth user of the private HLS preview page |

The show settings themselves (models, look, pacing, music, sign-off) are `SHOW_ENV` at the top of
`tools/preview.py`; the `.env` only adds keys it does not set.

## 4. Kick chat

On Kick's developer page, set the app's webhook to a public HTTPS address that nginx forwards to
`127.0.0.1:18795` (`KICK_WEBHOOK_PORT`), then run once:

```bash
.venv/bin/python tools/kick_subscribe.py
```

Every webhook is checked against Kick's signature. Hubie only reads chat; nothing in the repo posts to it.

## 5. Run

```bash
python3 tools/preview.py start           # private run: HLS to /dev/shm/hubie_preview/hls, nothing leaves the box
python3 tools/preview.py start --kick    # live: RTMPS to Kick, plus the same picture as the HLS preview
python3 tools/preview.py status          # parts, pids, RAM, Claude calls and $, ElevenLabs characters, show_over
python3 tools/preview.py stop            # stops the whole tree, game Chrome included
```

Remote output is refused unless `HUBIE_ALLOW_REMOTE_OUTPUT` holds the exact phrase in
`hubie/pipeline/output_guard.py`; `start --kick` sets it. Set `HUBIE_PREVIEW_ROOT` if the checkout is not at
`/root/hubie_stream`. Optional preview page: put `<token> <password>` in `data/preview_secret.txt` and serve
`deploy/preview/index.html` and the HLS folder from nginx under `/hubie/preview-<token>/`.

Once started it needs nothing: the guardian keeps the watchdog up, the watchdog restarts any part that dies or
freezes, and the game adapter relaunches the game Chrome. For a reboot-proof run, install
`deploy/hubie-stream.service` (systemd, `Restart=always`, `MemoryMax=3G`) around `python -m hubie run`.

## 6. How it ends

The show signs off by itself when the ElevenLabs balance drops to `HUBIE_SIGNOFF_CHARS` (2500) or at `HUBIE_END_AT`:
Hubie says goodbye, a show-over flag is written and nothing restarts. `python -m hubie new-show` clears the flag.

## Safety, in short

- Zero-bet house originals only: every request to the games server is checked (stake 0, allow-listed actions);
  anything else stops play for good and the show carries on as talk.
- Every spoken or captioned line passes the output filter: no win promises, predictions, deposit, bet or sign-up
  pushes, codes or links. Chat is moderated before it is shown or answered; hard moments get fixed lines.
- "18+ | Gamble responsibly | AI-generated character | Demo play, no real money" is burned into every frame.
