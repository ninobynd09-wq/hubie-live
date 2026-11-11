import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The older desk-scene tests check the desk1 layout (still there for rollback, HUBIE_LOOK=desk1); look H, the
# default on the stream, is covered by tests/test_look_h.py, which sets HUBIE_LOOK=h itself.
import os  # noqa: E402
os.environ.setdefault("HUBIE_LOOK", "desk1")


def pytest_configure(config):
    config.addinivalue_line("markers", "chaos: starts the real stack (Xvfb, Chrome, ffmpeg) and kills parts of it")
