"""The Files tab: box-files in the box (run here against a temporary home), the control page's
routes for her captures, and how files are served (never as a live page)."""
import http.client
import json
import os
import subprocess
import sys
from pathlib import Path

from talker.web_panel import WebPanel

ROOT = Path(__file__).resolve().parents[1]
BOX_FILES = ROOT / "box" / "box-files"


def box(home, req, data=b""):
    p = subprocess.run([sys.executable, str(BOX_FILES)], input=json.dumps(req).encode() + b"\n" + data,
                       capture_output=True, env={**os.environ, "HOME": str(home)})
    return p.returncode, p.stdout


def test_box_files_stays_inside_the_workspace(tmp_path):
    work = tmp_path / "work"
    (work / "site").mkdir(parents=True)
    (work / "site" / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "secret").write_text("key")
    (work / "escape").symlink_to(tmp_path / "secret")
    code, out = box(tmp_path, {"op": "list", "path": ""})
    assert code == 0 and {e["name"] for e in json.loads(out)["entries"]} == {"site", "escape"}
    assert box(tmp_path, {"op": "read", "path": "site/index.html"}) == (0, b"<h1>hi</h1>")
    for bad in ["../secret", "escape", "/etc/passwd", "site/../../secret"]:
        code, out = box(tmp_path, {"op": "read", "path": bad})
        assert code == 1 and "outside" in json.loads(out)["error"] or "not a file" in json.loads(out)["error"], bad
    assert box(tmp_path, {"op": "write", "path": "site", "name": "../x", "size": 1}, b"x")[0] == 1
    assert box(tmp_path, {"op": "write", "path": "site", "name": "up.txt", "size": 3}, b"abc")[0] == 0
    assert (work / "site" / "up.txt").read_bytes() == b"abc"
    assert box(tmp_path, {"op": "write", "path": "site", "name": "up.txt", "size": 1}, b"z")[0] == 1   # no silent overwrite
    assert box(tmp_path, {"op": "trash", "path": "escape"})[0] == 0          # the link goes, not its target
    assert (tmp_path / "secret").read_text() == "key"
    assert box(tmp_path, {"op": "rename", "path": "site/up.txt", "name": "notes.txt"})[0] == 0
    assert box(tmp_path, {"op": "trash", "path": ""})[0] == 1
    assert box(tmp_path, {"op": "empty-trash"})[0] == 0 and not (work / ".trash").exists()


def _call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def test_captures_on_the_page_and_safe_serving(tmp_path, monkeypatch):
    caps = tmp_path / "captures"
    caps.mkdir()
    (caps / "photo-1.jpg").write_bytes(b"\xff\xd8jpeg")
    (caps / "page.html").write_text("<script>alert(1)</script>")
    monkeypatch.setattr("shutil.which", lambda name: None)            # no gio: falls back to captures/.trash
    panel = WebPanel(port=0, host="127.0.0.1")
    panel.captures_dir = str(caps)
    panel.start()
    J = {"Content-Type": "application/json"}
    try:
        code, _, out = _call(panel.port, "GET", "/api/files?where=captures&path=")
        assert {e["name"] for e in json.loads(out)["entries"]} == {"photo-1.jpg", "page.html"}
        code, h, out = _call(panel.port, "GET", "/api/files/raw?where=captures&path=page.html")
        assert h["Content-Type"].startswith("text/plain") and "sandbox" in h["Content-Security-Policy"]
        assert h["X-Content-Type-Options"] == "nosniff"
        code, h, out = _call(panel.port, "GET", "/api/files/raw?where=captures&path=photo-1.jpg")
        assert h["Content-Type"] == "image/jpeg" and out == b"\xff\xd8jpeg"
        code, h, _ = _call(panel.port, "GET", "/api/files/raw?where=captures&path=photo-1.jpg&download=1")
        assert h["Content-Disposition"].startswith("attachment")
        assert _call(panel.port, "GET", "/api/files/raw?where=captures&path=../../etc/passwd")[0] == 404
        assert _call(panel.port, "POST", "/api/files/trash", json.dumps({"where": "captures", "path": "photo-1.jpg"}),
                     {"Content-Type": "text/plain"})[0] == 403                      # another page's form post
        code, _, out = _call(panel.port, "POST", "/api/files/trash",
                             json.dumps({"where": "captures", "path": "photo-1.jpg"}), J)
        assert "moved" in json.loads(out)["message"] and (caps / ".trash").is_dir()
        assert not (caps / "photo-1.jpg").exists()
        code, _, out = _call(panel.port, "POST", "/api/files/upload?path=&name=x.txt", b"abc",
                             {"Content-Type": "application/octet-stream", "Origin": "http://evil.example"})
        assert code == 403
    finally:
        panel.stop()


def test_box_usage(tmp_path):
    (tmp_path / "work" / ".trash").mkdir(parents=True)
    (tmp_path / "work" / "a.bin").write_bytes(b"x" * 1000)
    (tmp_path / "work" / ".trash" / "old").write_bytes(b"y" * 300)
    code, out = box(tmp_path, {"op": "usage"})
    u = json.loads(out)
    assert code == 0 and u["work"] == 1300 and u["trash"] == 300 and u["disk_total"] > u["disk_free"] > 0


def test_logs_are_browsable_and_this_sessions_log_is_left_alone(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "voice-old.log").write_text("[you] hello")
    (logs / "voice-now.log").write_text("[you] still talking")
    monkeypatch.setattr("shutil.which", lambda name: None)
    panel = WebPanel(port=0, host="127.0.0.1")
    panel.logs_dir, panel.log_in_use = str(logs), str(logs / "voice-now.log")
    J = {"Content-Type": "application/json"}
    panel.start()
    try:
        code, _, out = _call(panel.port, "GET", "/api/files?where=logs&path=")
        entries = {e["name"]: e for e in json.loads(out)["entries"]}
        assert entries["voice-now.log"]["in_use"] and not entries["voice-old.log"]["in_use"]
        code, h, out = _call(panel.port, "GET", "/api/files/raw?where=logs&path=voice-old.log")
        assert out == b"[you] hello" and h["Content-Type"].startswith("text/plain")
        _, _, out = _call(panel.port, "POST", "/api/files/trash", json.dumps({"where": "logs", "path": "voice-now.log"}), J)
        assert "still being written" in json.loads(out)["error"] and (logs / "voice-now.log").exists()
        _, _, out = _call(panel.port, "POST", "/api/files/trash", json.dumps({"where": "logs", "path": "voice-old.log"}), J)
        assert "moved" in json.loads(out)["message"] and not (logs / "voice-old.log").exists()
        assert _call(panel.port, "GET", "/api/files/raw?where=logs&path=../logs/../../x")[0] == 404
    finally:
        panel.stop()


def test_the_speaker_player_never_logs_what_she_says_or_sees(capsys):
    src = (ROOT / "tools" / "usb_speaker.py").read_text()
    ns = {}
    exec(compile(src.split("def main")[0], "usb_speaker", "exec"), ns)
    ns["show"](b'I (1123) XiaoEsp32S3Sense: photo 2048x1536: kept 103243 bytes of 1 / 2 / 3\n'
               b'{"success": true, "text": "PEOPLE: one person, camera on the desk"}\n'
               b'I (5) Application: << the camera saw someone\n'
               b'W (8) Display:      a person by the camera\n'
               b'I (6) StateMachine: State: listening -> speaking\n'
               b'E (7) Camera: capture failed\n')
    out = capsys.readouterr().out
    assert "PEOPLE" not in out and "saw someone" not in out and "a person" not in out
    assert "State: listening" in out and "photo 2048x1536" in out and "capture failed" in out
