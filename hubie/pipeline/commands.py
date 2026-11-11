"""Exact command lines for Xvfb, Chrome and ffmpeg."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..config import Config
from ..safety.overlay import STRIP_TEXT
from .output_guard import check_browser_url, check_output

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# The FIFO's format is fixed, so skip probing it. Probing a live pipe reads seconds of audio before the
# picture's clock starts, and the voice then lands that much later than the picture (measured 3.2 s).
AUDIO_NOPROBE = ("-probesize", "32", "-analyzeduration", "0")


def xvfb_cmd(cfg: Config) -> list[str]:
    return ["Xvfb", cfg.display, "-screen", "0", f"{cfg.width}x{cfg.height}x24", "-nolisten", "tcp", "-noreset"]


def browser_cmd(cfg: Config) -> list[str]:
    url = check_browser_url(cfg.stage_url + "/")
    return [cfg.chrome_bin,
            f"--user-data-dir={cfg.run_dir / 'chrome'}",
            "--no-sandbox",  # required when running as root; only ever opens the loopback stage page
            "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
            "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
            "--disable-translate", "--disable-gpu", "--password-store=basic", "--use-mock-keychain",
            "--noerrdialogs", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows", "--renderer-process-limit=1",
            "--js-flags=--max-old-space-size=256", "--disable-dev-shm-usage", "--mute-audio",
            f"--remote-debugging-port={cfg.stage_port + 1}",  # loopback only; used as a liveness probe
            f"--window-position=0,0", f"--window-size={cfg.width},{cfg.height}",
            "--kiosk", f"--app={url}"]


def strip_file(cfg: Config) -> Path:
    p = cfg.run_dir / "strip.txt"
    p.write_text(STRIP_TEXT)
    return p


def encoder_cmd(cfg: Config) -> list[str]:
    mode, target = check_output(cfg.output)
    txt = strip_file(cfg)
    h = cfg.height
    # Safety strip is burned in at encode time. The probe branch taps the picture
    # before the strip so a dead browser can't hide behind the fixed text.
    fc = (f"[0:v]split=2[v][p];"
          f"[p]fps=1,scale=160:90,format=gray[probe];"
          f"[v]drawbox=x=0:y={h - 34}:w={cfg.width}:h=34:color=black@0.85:t=fill,"
          f"drawtext=fontfile={FONT}:textfile={txt}:fontsize=19:fontcolor=white:x=(w-text_w)/2:y={h - 27}[vo];"
          f"[1:a]aresample=async=1000:first_pts=0[ao]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-progress", "pipe:1", "-stats_period", "1",
           "-f", "x11grab", "-framerate", str(cfg.fps), "-video_size", f"{cfg.width}x{cfg.height}",
           "-draw_mouse", "0", "-thread_queue_size", "128", "-i", f"{cfg.display}+0,0",
           "-f", "s16le", "-ar", "48000", "-ac", "2", "-thread_queue_size", "512",
           "-use_wallclock_as_timestamps", "1", *AUDIO_NOPROBE, "-i", str(cfg.fifo),
           "-filter_complex", fc,
           "-map", "[vo]", "-map", "[ao]",
           "-c:v", "libx264", "-preset", cfg.x264_preset, "-tune", "zerolatency", "-pix_fmt", "yuv420p",
           "-b:v", cfg.video_bitrate, "-maxrate", cfg.video_bitrate, "-bufsize", "5000k",
           "-g", str(cfg.fps * 2), "-keyint_min", str(cfg.fps * 2), "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    cmd += output_args(cfg, mode, target)
    cmd += ["-map", "[probe]", "-c:v", "pgm", "-f", "image2", "-update", "1", "-y", str(cfg.probe)]
    return cmd


HLS_TIME = 2          # seconds per segment, one keyframe interval
HLS_LIST = 6          # segments in the playlist: a ~12 s window, older ones are deleted


def output_args(cfg: Config, mode: str, target: str) -> list[str]:
    if mode == "file":
        Path(target).mkdir(parents=True, exist_ok=True)
        return ["-f", "segment", "-segment_time", str(cfg.segment_seconds), "-reset_timestamps", "1",
                "-strftime", "1", "-segment_format", "mpegts", str(Path(target) / "hubie_%Y%m%d_%H%M%S.ts")]
    if mode == "hls":
        # Private preview: a short live window in a local folder. Segment numbers start from the clock,
        # so a restarted encoder never reuses a name a player already has; the supervisor prunes leftovers.
        Path(target).mkdir(parents=True, exist_ok=True)
        return ["-f", "hls", "-hls_time", str(HLS_TIME), "-hls_list_size", str(HLS_LIST),
                "-hls_flags", "delete_segments+omit_endlist+independent_segments+temp_file+discont_start",
                "-hls_start_number_source", "epoch", "-hls_delete_threshold", "2",
                "-hls_segment_filename", str(Path(target) / "seg_%d.ts"), str(Path(target) / "index.m3u8")]
    mirror = os.environ.get("HUBIE_OUTPUT_MIRROR_HLS", "")
    if mirror.startswith("/") and "://" not in mirror:
        # Live on Kick and still watchable on the private preview page: one encode, two outputs. The HLS side
        # may fail without touching the stream; the RTMP side failing ends ffmpeg so the watchdog reconnects.
        Path(mirror).mkdir(parents=True, exist_ok=True)
        hls = (f"[f=hls:onfail=ignore:hls_time={HLS_TIME}:hls_list_size={HLS_LIST}"
               ":hls_flags=delete_segments+omit_endlist+independent_segments+temp_file+discont_start"
               f":hls_start_number_source=epoch:hls_delete_threshold=2"
               f":hls_segment_filename={Path(mirror) / 'seg_%d.ts'}]{Path(mirror) / 'index.m3u8'}")
        return ["-f", "tee", "-flags", "+global_header", f"[f=flv:flvflags=no_duration_filesize]{target}|{hls}"]
    return ["-f", "flv", target]


def _text(cfg: Config, name: str, x: str, y: str, size: int, color: str = "white", spacing: int = 6) -> str:
    # expansion=none: this text comes from chat and the model, a % in it must stay a %.
    return (f"drawtext=fontfile={FONT}:textfile={cfg.scene_dir / (name + '.txt')}:reload=1:expansion=none:"
            f"fontsize={size}:fontcolor={color}:line_spacing={spacing}:x={x}:y={y}")


def scene_cmd(cfg: Config) -> list[str]:
    """720p picture with no browser. The background is the art, or later the game browser on the display.
    Hubie, captions, chat and the safety strip are drawn here, so they survive the game dying."""
    from ..stage import desk
    if desk.desk_ready(cfg):
        return desk_cmd(cfg)
    mode, target = check_output(cfg.output)
    txt = strip_file(cfg)
    h = cfg.height
    if cfg.scene_bg == "x11":
        base = ["-f", "x11grab", "-framerate", str(cfg.fps), "-video_size", f"{cfg.width}x{cfg.height}",
                "-draw_mouse", "0", "-thread_queue_size", "128", "-i", f"{cfg.display}+0,0"]
    else:
        base = ["-re", "-f", "lavfi", "-i", f"color=c=0x0b1a14:s={cfg.width}x{cfg.height}:r={cfg.fps}"]
    panel = "[0:v][2:v]overlay=eof_action=repeat[bg];" if cfg.scene_bg != "x11" else "[0:v]null[bg];"
    fc = (panel +
          "[3:v]format=rgba[hubie];"
          "[bg][hubie]overlay=x=950:y=340+5*sin(t*1.4):eof_action=repeat,"
          + ",".join([
              _text(cfg, "segment", "36", "56", 22, "0xbfe8d0"),
              _text(cfg, "game", "36", "150", 46, spacing=14),
              _text(cfg, "history", "36", "436", 18, "0x9fc4b0"),
              _text(cfg, "chat", "856", "56", 16, "0xdfeee6", 5),
              _text(cfg, "caption", "40", "500", 25, spacing=9),
              f"drawtext=fontfile={FONT}:text='%{{gmtime\\:%H\\\\\\:%M\\\\\\:%S}} UTC':fontsize=14:fontcolor=0x7f9f90:x=w-tw-28:y=30",
          ]) +
          ",split=2[v][p];"
          "[p]fps=1,scale=160:90,format=gray[probe];"
          f"[v]drawbox=x=0:y={h - 34}:w={cfg.width}:h=34:color=black@0.85:t=fill,"
          f"drawtext=fontfile={FONT}:textfile={txt}:fontsize=19:fontcolor=white:x=(w-text_w)/2:y={h - 27}[vo];"
          "[1:a]aresample=async=1000:first_pts=0[ao]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-progress", "pipe:1", "-stats_period", "1",
           *base,
           "-f", "s16le", "-ar", "48000", "-ac", "2", "-thread_queue_size", "512",
           "-use_wallclock_as_timestamps", "1", *AUDIO_NOPROBE, "-i", str(cfg.fifo),
           "-i", str(cfg.art_dir / "background.png"),
           "-re", "-f", "image2", "-loop", "1", "-framerate", "15", "-i", str(cfg.scene_dir / "hubie.png"),
           "-filter_complex", fc,
           "-map", "[vo]", "-map", "[ao]",
           "-c:v", "libx264", "-preset", cfg.x264_preset, "-tune", "zerolatency", "-pix_fmt", "yuv420p",
           "-b:v", cfg.video_bitrate, "-maxrate", cfg.video_bitrate, "-bufsize", "5000k",
           "-g", str(cfg.fps * 2), "-keyint_min", str(cfg.fps * 2), "-sc_threshold", "0",
           "-threads", "2", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    cmd += output_args(cfg, mode, target)
    cmd += ["-map", "[probe]", "-c:v", "pgm", "-f", "image2", "-update", "1", "-y", str(cfg.probe)]
    return cmd


def strip_pipes(font: str, size: int) -> list[tuple[int, int]]:
    """Where each "|" of the strip text starts (x from the text's left edge, px) and the whole text's width, as
    FreeType lays it out (advance widths, the way drawtext does), so a teal "|" lands exactly on each white one."""
    try:
        from PIL import ImageFont
        f = ImageFont.truetype(font, size, layout_engine=ImageFont.Layout.BASIC)
    except Exception:
        return []
    out = []
    for i, c in enumerate(STRIP_TEXT):
        if c == "|":
            out.append(int(f.getlength(STRIP_TEXT[:i])))
    tw = int(f.getlength(STRIP_TEXT))
    return [(x, tw) for x in out]


_PIPES_FF: dict = {}


def _drawn_cols(font: str, size: int, text: str, w: int = 2000):
    """Grey rendering of `text` by drawtext at x=0 (numpy, rows x w)."""
    import tempfile

    import numpy as np
    with tempfile.TemporaryDirectory() as td:
        t = Path(td) / "s.txt"
        t.write_text(text, encoding="utf-8")
        h = size * 3 // 2 * 2
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={w}x{h}", "-vf",
                            f"drawtext=fontfile={font}:textfile={t}:fontsize={size}:fontcolor=white:x=0:y={size}",
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, timeout=20)
        return np.frombuffer(r.stdout, np.uint8).reshape(h, w)


def strip_pipes_drawn(font: str, size: int) -> list[tuple[int, int]]:
    """Like strip_pipes, but measured on drawtext's own rendering (hinting and kerning make Inter ~5% narrower in
    drawtext than PIL's advances): the strip text drawn once, each "|" found as a full-height column, minus where a
    lone "|" puts its ink. Falls back to strip_pipes when anything doesn't add up."""
    key = (font, size)
    if key in _PIPES_FF:
        return _PIPES_FF[key]
    out = []
    try:
        import numpy as np

        def tall(a):
            xs = [x for x in range(a.shape[1]) if (a[:, x] > 100).sum() >= size - 1]
            return [x for i, x in enumerate(xs) if i == 0 or x - xs[i - 1] > 1]
        a = _drawn_cols(font, size, STRIP_TEXT)
        cols, lone = tall(a), tall(_drawn_cols(font, size, "|", 200))
        ink = np.where(a.max(0) > 100)[0]
        if len(cols) == STRIP_TEXT.count("|") and len(lone) == 1 and len(ink):
            out = [(x - lone[0], int(ink.max()) + 1) for x in cols]
    except Exception:
        out = []
    _PIPES_FF[key] = out or strip_pipes(font, size)
    return _PIPES_FF[key]


def _strip_and_out(cfg: Config, scene: str, audio: int = 1) -> str:
    """Probe tap before the strip, then the fixed safety strip burned in. Same for every layout."""
    txt = strip_file(cfg)
    h, sh = cfg.height, 34 if cfg.height <= 720 else round(34 * cfg.height / 720)
    fs = 19 if cfg.height <= 720 else round(19 * cfg.height / 720)
    from ..stage import desk
    if desk.look_name() == "mix":
        # look mix (setup 06's strip): near-black band, thin slate line on top, Inter SemiBold, ice-blue "|" separators
        from ..stage import look_mix as MX
        font = str(MX.FONTS["is"]) if MX.FONTS["is"].exists() else FONT
        fs = 15 if cfg.height <= 720 else round(15 * cfg.height / 720)
        y = h - sh + (sh - fs) // 2
        pipes = "".join(f",drawtext=fontfile={font}:text='|':fontsize={fs}:fontcolor=0x96dcff:x=(w-{tw})/2+{px}:y={y}"
                        for px, tw in strip_pipes_drawn(font, fs))
        return (f"[{scene}]split=2[v][p];"
                "[p]fps=1,scale=160:90,format=gray[probe];"
                f"[v]drawbox=x=0:y={h - sh}:w={cfg.width}:h={sh}:color=0x03060a@0.92:t=fill,"
                f"drawbox=x=0:y={h - sh}:w={cfg.width}:h=1:color=0x283846@1:t=fill,"
                f"drawtext=fontfile={font}:textfile={txt}:fontsize={fs}:fontcolor=0xf0f5f8:x=(w-text_w)/2:y={y}"
                f"{pipes}[vo];"
                f"[{audio}:a]aresample=async=1000:first_pts=0[ao]")
    if desk.look_name() == "h":
        # look H: solid near-black band with a thin teal line on top, Poppins Bold, the "|" separators teal.
        # The exact text is still the one drawtext of strip.txt; the teal pipes are drawn over its own pipes.
        font = desk.POPPINS if Path(desk.POPPINS).exists() else FONT
        y = h - sh + (sh - fs) // 2 - 1
        pipes = "".join(f",drawtext=fontfile={font}:text='|':fontsize={fs}:fontcolor=0x40e0d0:x=(w-{tw})/2+{px}:y={y}"
                        for px, tw in strip_pipes(font, fs))
        return (f"[{scene}]split=2[v][p];"
                "[p]fps=1,scale=160:90,format=gray[probe];"
                f"[v]drawbox=x=0:y={h - sh}:w={cfg.width}:h={sh}:color={desk.H_STRIP_FILL}@1:t=fill,"
                f"drawbox=x=0:y={h - sh}:w={cfg.width}:h=1:color={desk.H_STRIP_LINE}@1:t=fill,"
                f"drawtext=fontfile={font}:textfile={txt}:fontsize={fs}:fontcolor=white:x=(w-text_w)/2:y={y}"
                f"{pipes}[vo];"
                f"[{audio}:a]aresample=async=1000:first_pts=0[ao]")
    return (f"[{scene}]split=2[v][p];"
            "[p]fps=1,scale=160:90,format=gray[probe];"
            f"[v]drawbox=x=0:y={h - sh}:w={cfg.width}:h={sh}:color=black@0.85:t=fill,"
            f"drawtext=fontfile={FONT}:textfile={txt}:fontsize={fs}:fontcolor=white:x=(w-text_w)/2:y={h - sh + (sh - fs) // 2 - 1}[vo];"
            f"[{audio}:a]aresample=async=1000:first_pts=0[ao]")


def desk_cmd(cfg: Config) -> list[str]:
    """The desk layout (hubie/stage/desk.py): game, desk cam with idle/talk loops, subtitles, chat list, stats.
    Input 0 is a steady clock so the picture keeps going whatever the game surface does."""
    from ..stage import desk
    mode, target = check_output(cfg.output)
    desk.prepare(cfg)
    base = ["-re", "-f", "lavfi", "-i", f"color=c=0x061017:s={cfg.width}x{cfg.height}:r={cfg.fps}"]
    ins, fc = desk.filter_inputs(cfg, base, first=1)
    # The audio FIFO goes last: its timestamps are wall clock from when it is first read, and every input
    # opened after it would push the picture's clock later (the mouth ran 3 s ahead of the voice that way).
    audio = 1 + sum(1 for x in ins if x == "-i")
    fc += ";" + _strip_and_out(cfg, "scene", audio)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-progress", "pipe:1", "-stats_period", "1",
           *base,
           *ins,
           "-f", "s16le", "-ar", "48000", "-ac", "2", "-thread_queue_size", "512",
           "-use_wallclock_as_timestamps", "1", *AUDIO_NOPROBE, "-i", str(cfg.fifo),
           "-filter_complex", fc,
           "-map", "[vo]", "-map", "[ao]",
           "-c:v", "libx264", "-preset", cfg.x264_preset, "-tune", "zerolatency", "-pix_fmt", "yuv420p",
           "-b:v", cfg.video_bitrate, "-maxrate", cfg.video_bitrate, "-bufsize", "5000k",
           "-g", str(cfg.fps * 2), "-keyint_min", str(cfg.fps * 2), "-sc_threshold", "0",
           "-threads", "2", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    cmd += output_args(cfg, mode, target)
    cmd += ["-map", "[probe]", "-c:v", "pgm", "-f", "image2", "-update", "1", "-y", str(cfg.probe)]
    return cmd
