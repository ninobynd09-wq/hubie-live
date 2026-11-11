"""All settings come from environment variables; nothing secret lives in the repo."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader. Existing environment wins."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _grab(v: str) -> tuple | None:
    try:
        x, y, w, h = (int(p) for p in v.split(","))
        return (x, y, w // 2 * 2, h // 2 * 2) if w > 0 and h > 0 else None
    except ValueError:
        return None


@dataclass
class Config:
    run_dir: Path = field(default_factory=lambda: Path(_env("HUBIE_RUN_DIR", "/dev/shm/hubie_stream")))
    data_dir: Path = field(default_factory=lambda: Path(_env("HUBIE_DATA_DIR", str(ROOT / "data"))))
    display: str = field(default_factory=lambda: _env("HUBIE_DISPLAY", ":87"))
    width: int = field(default_factory=lambda: int(_env("HUBIE_WIDTH", "1280")))
    height: int = field(default_factory=lambda: int(_env("HUBIE_HEIGHT", "720")))
    fps: int = field(default_factory=lambda: int(_env("HUBIE_FPS", "30")))
    stage_port: int = field(default_factory=lambda: int(_env("HUBIE_STAGE_PORT", "18790")))
    output: str = field(default_factory=lambda: _env("HUBIE_OUTPUT", f"file:{ROOT / 'recordings'}"))
    video_bitrate: str = field(default_factory=lambda: _env("HUBIE_VIDEO_BITRATE", "2500k"))
    x264_preset: str = field(default_factory=lambda: _env("HUBIE_X264_PRESET", "veryfast"))
    segment_seconds: int = field(default_factory=lambda: int(_env("HUBIE_SEGMENT_SECONDS", "600")))
    record_max_gb: float = field(default_factory=lambda: float(_env("HUBIE_RECORD_MAX_GB", "5")))
    scene: str = field(default_factory=lambda: _env("HUBIE_SCENE", "chrome"))   # chrome | ffmpeg
    scene_bg: str = field(default_factory=lambda: _env("HUBIE_SCENE_BG", "art"))   # art | image (still game picture, for tests) | x11 (game browser on the display)
    scene_bg_image: Path = field(default_factory=lambda: Path(_env("HUBIE_SCENE_BG_IMAGE", str(ROOT / "shots/funmode_game_1280.png"))))
    layout: str = field(default_factory=lambda: _env("HUBIE_LAYOUT", "desk"))   # desk | classic (ffmpeg scene only)
    cam_idle: Path = field(default_factory=lambda: Path(_env("HUBIE_CAM_IDLE", str(ROOT / "shots/kling_idle_v1.mp4"))))
    cam_talk: Path = field(default_factory=lambda: Path(_env("HUBIE_CAM_TALK", str(ROOT / "shots/kling_talk_v2.mp4"))))
    cam_talk_fallback: Path = field(default_factory=lambda: Path(_env("HUBIE_CAM_TALK_FALLBACK", str(ROOT / "shots/kling_talk_v1.mp4"))))
    cam_mode: str = field(default_factory=lambda: _env("HUBIE_CAM_MODE", "lipsync"))   # lipsync | switch
    mouth_dir: Path = field(default_factory=lambda: Path(_env("HUBIE_MOUTH_DIR", str(ROOT / "assets/cam_visemes"))))
    lipsync_offset_ms: float = field(default_factory=lambda: float(_env("HUBIE_LIPSYNC_OFFSET_MS", "-60")))
    mouth_port: int = field(default_factory=lambda: int(_env("HUBIE_MOUTH_PORT", "18793")))
    cam_zoom: float = field(default_factory=lambda: float(_env("HUBIE_CAM_ZOOM", "1.18")))
    react_win: Path = field(default_factory=lambda: Path(_env("HUBIE_REACT_WIN", str(ROOT / "shots/kling_react_win_v1.mp4"))))
    react_loss: Path = field(default_factory=lambda: Path(_env("HUBIE_REACT_LOSS", str(ROOT / "shots/kling_react_loss_v1.mp4"))))
    react_surprise: Path = field(default_factory=lambda: Path(_env("HUBIE_REACT_SURPRISE", str(ROOT / "shots/kling_react_surprise_v1.mp4"))))
    react_banana: Path = field(default_factory=lambda: Path(_env("HUBIE_REACT_BANANA", str(ROOT / "shots/kling_react_banana_v1.mp4"))))
    cam_fade_ms: float = field(default_factory=lambda: float(_env("HUBIE_CAM_FADE_MS", "200")))
    # spin click clip: a mouth set of the click (mouth_0..8.mp4, same shapes as HUBIE_MOUTH_DIR); empty = no click.
    # The claw lands HUBIE_CLICK_LAND_S into the clip; the adapter's real click waits for that moment.
    click_mouth_dir: str = field(default_factory=lambda: _env("HUBIE_CLICK_MOUTH_DIR", ""))
    # paw: only the held paw + mouse area (HUBIE_CLICK_PAW, an ellipse box x0,y0,x1,y1,feather in source pixels, the same
    # one kling/scripts/hold_region.py froze in the idle) is laid over the live idle: any frame, face untouched.
    # full: the whole picture with the click mouth set, cross-faded, started where the idle is closest.
    click_mode: str = field(default_factory=lambda: _env("HUBIE_CLICK_MODE", "paw"))
    click_paw: str = field(default_factory=lambda: _env("HUBIE_CLICK_PAW", "60,440,500,760,22"))
    click_land_s: float = field(default_factory=lambda: float(_env("HUBIE_CLICK_LAND_S", "0.833")))
    click_max_wait: float = field(default_factory=lambda: float(_env("HUBIE_CLICK_MAX_WAIT", "0.5")))
    click_offset_ms: float = field(default_factory=lambda: float(_env("HUBIE_CLICK_OFFSET_MS", "0")))
    react_match: float = field(default_factory=lambda: float(_env("HUBIE_REACT_MATCH", "8")))
    game_grab: tuple | None = field(default_factory=lambda: _grab(_env("HUBIE_GAME_GRAB", "")))   # x,y,w,h on the display
    art_dir: Path = field(default_factory=lambda: Path(_env("HUBIE_ART_DIR", str(ROOT / "assets/placeholder"))))
    disk_floor_gb: float = field(default_factory=lambda: float(_env("HUBIE_DISK_FLOOR_GB", "8")))
    chrome_bin: str = field(default_factory=lambda: _env("HUBIE_CHROME_BIN", "google-chrome"))
    persona: Path = field(default_factory=lambda: Path(_env("HUBIE_PERSONA", str(ROOT / "persona/hubie.draft.yaml"))))
    provider: str = field(default_factory=lambda: _env("HUBIE_PROVIDER", "mock"))
    tts: str = field(default_factory=lambda: _env("HUBIE_TTS", "flite"))
    chat: str = field(default_factory=lambda: _env("HUBIE_CHAT", "fake"))
    game: str = field(default_factory=lambda: _env("HUBIE_GAME", "fake"))
    fake_chat_per_min: float = field(default_factory=lambda: float(_env("HUBIE_FAKE_CHAT_PER_MIN", "6")))
    night_offset_hours: float = field(default_factory=lambda: float(_env("HUBIE_LOCAL_OFFSET_HOURS", "0")))
    test_hooks: bool = field(default_factory=lambda: _env("HUBIE_TEST_HOOKS", "0") == "1")

    # Watchdog thresholds, seconds. Tests shorten nothing that changes logic.
    wd_tick: float = field(default_factory=lambda: float(_env("HUBIE_WD_TICK", "1")))
    wd_start_grace: float = field(default_factory=lambda: float(_env("HUBIE_WD_START_GRACE", "20")))
    wd_status_stale: float = field(default_factory=lambda: float(_env("HUBIE_WD_STATUS_STALE", "8")))
    wd_brain_stale: float = field(default_factory=lambda: float(_env("HUBIE_WD_BRAIN_STALE", "40")))
    wd_encoder_stale: float = field(default_factory=lambda: float(_env("HUBIE_WD_ENCODER_STALE", "8")))
    wd_frozen_picture: float = field(default_factory=lambda: float(_env("HUBIE_WD_FROZEN", "12")))
    wd_page_stale: float = field(default_factory=lambda: float(_env("HUBIE_WD_PAGE_STALE", "10")))
    wd_dead_air: float = field(default_factory=lambda: float(_env("HUBIE_WD_DEAD_AIR", "30")))
    wd_max_backoff: float = field(default_factory=lambda: float(_env("HUBIE_WD_MAX_BACKOFF", "30")))
    wd_rss_browser_mb: int = field(default_factory=lambda: int(_env("HUBIE_WD_RSS_BROWSER_MB", "1500")))
    wd_rss_encoder_mb: int = field(default_factory=lambda: int(_env("HUBIE_WD_RSS_ENCODER_MB", "800")))
    wd_rss_brain_mb: int = field(default_factory=lambda: int(_env("HUBIE_WD_RSS_BRAIN_MB", "500")))

    @property
    def status_dir(self) -> Path:
        return self.run_dir / "status"

    @property
    def pid_dir(self) -> Path:
        return self.run_dir / "pids"

    @property
    def log_dir(self) -> Path:
        return self.run_dir / "logs"

    @property
    def scene_dir(self) -> Path:
        return self.run_dir / "scene"

    @property
    def queue_dir(self) -> Path:
        return self.run_dir / "audio_queue"

    @property
    def fifo(self) -> Path:
        return self.run_dir / "audio.pcm"

    @property
    def probe(self) -> Path:
        return self.run_dir / "probe.pgm"

    @property
    def show_over_flag(self) -> Path:
        """Written once the show has signed off: supervisor and guardian exit and restart nothing (hubie/credits.py)."""
        return self.run_dir / "show_over.json"

    @property
    def incidents(self) -> Path:
        return self.data_dir / "watchdog.jsonl"

    @property
    def stage_url(self) -> str:
        return f"http://127.0.0.1:{self.stage_port}"

    def ensure_dirs(self) -> None:
        for d in (self.run_dir, self.status_dir, self.pid_dir, self.log_dir,
                  self.queue_dir, self.data_dir):
            d.mkdir(parents=True, exist_ok=True)
