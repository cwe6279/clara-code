"""No photos, scans, recordings or logs are tracked (tools/no_captures.py, also the pre-commit hook)."""
import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("no_captures", ROOT / "tools" / "no_captures.py")
N = importlib.util.module_from_spec(spec)
spec.loader.exec_module(N)


def test_nothing_tracked_looks_like_a_capture():
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=ROOT).stdout.split()
    allow = N.allowed_globs(ROOT)
    assert [f for f in files if N.why_refused(f, allow)] == []


def test_the_rules():
    allow = list(N.DEFAULT_ALLOW)
    for bad in ["captures/photo-20261118-1059.jpg", "x/recordings/a.txt", "logs/voice.log", "selfie.png",
                "tools/scan-1.pdf", "last-look.jpg", "emergencies/1/note.json", "talker/clip.wav"]:
        assert N.why_refused(bad, allow), bad
    for ok in ["faces/clara/eye_left.png", "docs/diagram.png", "talker/voice_loop.py", "README.md"]:
        assert not N.why_refused(ok, allow), ok
