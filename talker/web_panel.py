"""
web_panel.py — a small control page for the running character.

On by default at http://localhost:8020 (voice_loop.py --web-port, --web-host, --no-web);
if that port is taken it walks up to the next free one and prints the URL.

It binds to this machine only. There is no authentication, and the page serves a
camera snapshot, a Wi-Fi scan and a Wi-Fi join endpoint, so --web-host 0.0.0.0
hands all of that to anyone on the network. A kiosk needs it; a laptop does not.
Standard library only, one daemon thread, nothing on the audio or render path:
the page polls /api/state twice a second and the loop never waits for it.

What it shows and does:
  * status: face, ears, brain, voice, mic level, speaking / thinking / engaged,
    the last [turn] timing line, the last thing heard and said, vision stats
  * live tunables with their help text: silence gate, barge-in, idle timeout,
    vision interval and change filter, debug overlay ... applied at once
  * tests for the physical setup: mic level meter, speak a line through the
    speaker, send a line as a visitor, interrupt, a camera snapshot, and the
    calibration routine (room, speaker bleed, a person) with its verdict
  * the full flag reference (every --option with its help and current value)
    and the face.json fields with what they do
  * Wi-Fi (Raspberry Pi kiosks): see networks and join one through NetworkManager
  * Agent (assistant faces with errands): the backend agent's address, a reachability
    test, saving it to settings.json, and the task list with each summary

Register what the page may touch from voice_loop.py: panel.tunable(...),
panel.action(...), panel.status_fn, panel.calibrator, panel.snapshot, panel.agent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8020


@dataclass
class Tunable:
    name: str
    get: Callable[[], Any]
    set: Callable[[Any], None]
    help: str
    kind: str = "float"           # int | float | bool | str
    unit: str = ""
    lo: Optional[float] = None
    hi: Optional[float] = None
    flag: str = ""                # the command-line flag that sets the same thing at startup
    options: Optional[List[str]] = None   # a fixed set of values: shown as a dropdown

    def coerce(self, raw: Any) -> Any:
        if self.kind == "bool":
            return str(raw).lower() in ("1", "true", "on", "yes")
        if self.kind == "int":
            v: Any = int(float(raw))
        elif self.kind == "float":
            v = float(raw)
        else:
            if self.options:
                match = [o for o in self.options if o.lower() == str(raw).strip().lower()]
                if not match:
                    raise ValueError(f"{self.name} must be one of {', '.join(self.options)}")
                return match[0]
            return str(raw)
        if self.lo is not None:
            v = max(self.lo, v)
        if self.hi is not None:
            v = min(self.hi, v)
        return v


@dataclass
class Action:
    name: str
    fn: Callable[[str], str]      # takes the optional text field, returns a message
    help: str
    takes_text: bool = False


# ── face.json field help (the "?" text next to each manifest value) ────────────
FACE_FIELD_HELP = {
    "canvas_w": "Canvas width in pixels. Procedural shapes scale with it; set 1080 for a pixel-exact projector.",
    "canvas_h": "Canvas height.", "fps": "Render rate; 30 halves the work on a Pi. TALKER_FPS overrides.",
    "bg_color": "Background colour; keep black for projection.",
    "glow_color": "Halo colour around procedural shapes and the ambient glow behind body art.",
    "glow_intensity": "0 disables the glow, 1 normal, 2 intense.",
    "glow_style": "halo: soft light spills outward. inner: crisp cut edges lit from inside (pumpkin).",
    "core_color": "inner style: the hot spot colour (default: the shape colour pushed toward white).",
    "rim_color": "inner style: thin cut-edge line.", "light_offset": "inner style: moves the hot spot down.",
    "cut_depth": "inner style: [dx, dy] inset; the shell's inner wall shows on the other side.",
    "wall_color": "inner style: colour of that inner wall.",
    "eye_left": "cx, cy centre; image, scale, opacity for PNG eyes.", "eye_right": "Same for the right eye.",
    "eye_color": "Procedural eye colour.", "draw_eyes": "false: no eyes at all (a voice-only character).",
    "blink": "Blink on or off.", "blink_interval": "[min, max] seconds between blinks; emotions scale it.",
    "blink_speed": "Higher is snappier.", "eye_speech_pulse": "Eyes grow by this fraction when the mouth is wide open.",
    "eye_lids": "Image eyes without pupils: emotions as lid cuts, blinks close the lids.",
    "gaze_amount": "Idle glance distance in px.", "gaze_interval": "[min, max] seconds between glances.",
    "gaze_speed": "Easing rate of a glance.", "gaze_while_speaking": "0 locks the eyes forward while talking.",
    "textured_eye": "Live eyes composed from parts in a folder: pupil moves, dilates, lids track.",
    "mouth": "anchor_cx/cy, min_w/max_w, style toothed | rounded | grin, n_teeth, color, opacity (0 = no mouth).",
    "mouth_images": "viseme key to PNG; six images cover all twelve shapes.",
    "draw_nose": "Procedural triangle nose.", "nose": "Nose PNG placement.",
    "draw_stem": "Pumpkin stem.", "voices": "Default voice per backend: elevenlabs id, edge name, piper voice.",
    "models": "This face's brain per --llm: {\"claude\": \"claude-haiku-4-5\"}. --model wins. Add -fast to an Opus model for fast mode.",
    "tts": "This face's own voice backend when --tts is not given: piper (local), elevenlabs, edge, fish.",
    "tts_model": "ElevenLabs model for this face (v3 performs tags, flash is faster).",
    "voice_speed": "Speaking rate multiplier.", "wake_words": "Names that start a conversation in wake mode.",
    "sleep_words": "Short phrases that end it at once.", "sounds": "Folder of sound effects for {{sfx name}}.",
    "body": "moves: the movement names the brain may ask for with {{move name}}.",
    "character": "Personality text (from character.md).",
    "memory": "true: a working memory in notes.md and tasks.md next to face.json, written with {{note ...}} and read at launch through {notes} and {tasks} in character.md.",
    "errands": "true, or a list of what the backend agent can do: {{task ...}} hands work to it at AGENT_RELAY_URL; the result is announced when the room is quiet. Listing its abilities makes her hand those off instead of saying she cannot.",
    "dictation_words": "Short phrases that switch dictation on: keep transcribing, answer only after a long pause.",
    "dictation_end_words": "Short phrases that switch dictation off and answer what was dictated.",
}


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def mdns_name() -> Optional[str]:
    """This machine's <hostname>.local, if Avahi/Bonjour is likely to resolve it.

    On a kiosk the IP moves but the name does not, and phones and laptops resolve
    .local out of the box, so this is the address worth writing on the box itself.
    """
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        return None
    return f"{host}.local" if host and host != "localhost" else None


def describe_parser(parser, args) -> List[Dict[str, Any]]:
    """Every --flag with its help text and the value in use this run."""
    rows = []
    for a in parser._actions:
        if not a.option_strings or a.dest in ("help",):
            continue
        h = a.help or ""
        try:                                   # argparse style: %(default)s, %% for a literal percent
            h = h % {"default": a.default}
        except (KeyError, TypeError, ValueError):
            h = h.replace("%%", "%")
        rows.append({"flags": ", ".join(a.option_strings), "help": h,
                     "default": None if a.default is None else str(a.default),
                     "value": None if args is None else str(getattr(args, a.dest, None))})
    return rows


# ── Wi-Fi through NetworkManager (Raspberry Pi OS and most Linux) ───────────────
TEXT_EXTS = {".txt", ".md", ".json", ".csv", ".tsv", ".log", ".py", ".js", ".ts", ".html", ".htm", ".css",
             ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".sql", ".jsonl", ".svg"}


class WifiControl:
    def __init__(self):
        self.available = shutil.which("nmcli") is not None

    def _run(self, *cmd: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run(["nmcli", "-t", *cmd], capture_output=True, text=True, timeout=timeout)

    def status(self) -> Dict[str, Any]:
        if not self.available:
            return {"available": False, "reason": "nmcli (NetworkManager) is not installed on this box"}
        devs = []
        r = self._run("-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
        for line in r.stdout.splitlines():
            parts = self._fields(line)
            if len(parts) >= 4 and parts[1] in ("wifi", "ethernet"):
                devs.append({"device": parts[0], "type": parts[1], "state": parts[2], "connection": parts[3]})
        return {"available": True, "devices": devs, "ip": lan_ip()}

    @staticmethod
    def _fields(line: str) -> List[str]:
        """nmcli -t separates fields with ':' and escapes a ':' inside a value as '\\:'."""
        return [f.replace("\\:", ":") for f in re.split(r"(?<!\\):", line)]

    def scan(self) -> List[Dict[str, Any]]:
        r = self._run("-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes", timeout=40)
        seen: Dict[str, Dict[str, Any]] = {}
        for line in r.stdout.splitlines():
            parts = self._fields(line)
            if len(parts) < 4:
                continue
            active, ssid, signal, sec = parts[0], parts[1], parts[2], parts[3]
            if not ssid:
                continue
            row = {"ssid": ssid, "signal": int(signal or 0), "security": sec or "open", "active": active == "yes"}
            if ssid not in seen or row["signal"] > seen[ssid]["signal"]:
                seen[ssid] = row
        return sorted(seen.values(), key=lambda x: (-x["active"], -x["signal"]))

    def connect(self, ssid: str, password: str = "") -> str:
        cmd = ["device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        r = self._run(*cmd, timeout=60)
        out = (r.stdout + r.stderr).strip()
        return out or ("connected" if r.returncode == 0 else f"failed ({r.returncode})")


# ── the panel ─────────────────────────────────────────────────────────────────
class WebPanel:
    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.tunables: Dict[str, Tunable] = {}
        self.actions: Dict[str, Action] = {}
        self.events: Deque[Dict[str, Any]] = deque(maxlen=80)
        self.status_fn: Callable[[], Dict[str, Any]] = lambda: {}
        self.parser = None
        self.args = None
        self.manifest = None
        self.snapshot: Optional[Callable[[], Optional[bytes]]] = None
        self.calibrator: Optional[Callable[[Callable[[str], None]], Dict]] = None
        self.pause: Callable[[bool], None] = lambda on: None
        self.agent = None                 # errands.AgentControl when the face hands work to an agent
        self.dreams_dir: Optional[str] = None   # dream.py reports, listed on the Dreams tab
        self.boards = None                # satellite.SatelliteServer when a board is the mic and speaker
        self.box_url = None               # () -> the Claude Code relay's address: the Box tab asks it
        self.captures_dir: Optional[str] = None   # her photos and scans: the Files tab's "Captures"
        self.logs_dir: Optional[str] = None       # session logs (conversation transcripts): "Logs"
        self.log_in_use: Optional[str] = None     # this run's own log: not renamed or deleted under it
        from .settings_store import SettingsStore
        # the Settings tab: read-only when the page is open to the network
        self.settings = SettingsStore(writable=host in ("127.0.0.1", "localhost", "::1"))
        self.wifi = WifiControl()
        self.calib: Dict[str, Any] = {"state": "idle", "prompt": "", "result": None, "error": ""}
        self._calib_go = threading.Event()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.started_at = time.time()

    # registration -------------------------------------------------------------
    def tunable(self, name: str, get, set, help: str, kind: str = "float", unit: str = "",
                lo: Optional[float] = None, hi: Optional[float] = None, flag: str = "",
                options: Optional[List[str]] = None) -> None:
        self.tunables[name] = Tunable(name, get, set, help, kind, unit, lo, hi, flag, list(options) if options else None)

    def action(self, name: str, fn: Callable[[str], str], help: str, takes_text: bool = False) -> None:
        self.actions[name] = Action(name, fn, help, takes_text)

    # files: her captures here, the box's workspace through the relay -------------------
    FILE_MAX = 25 * 1024 * 1024

    def _relay_raw(self, method: str, path: str, data: Optional[bytes] = None, ctype: str = "application/json"):
        """(status, bytes) from the relay, which alone holds the box's key."""
        import urllib.error
        import urllib.request
        url = self.box_url() if self.box_url else ""
        if not url:
            return 400, json.dumps({"error": "no Claude Code relay is set (--agent-url), so the box cannot be reached"}).encode()
        headers = {"Content-Type": ctype} if data is not None else {}
        tok = os.environ.get("AGENT_RELAY_TOKEN", "").strip()
        if tok:
            headers["X-Relay-Token"] = tok
        req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            return 502, json.dumps({"error": f"the relay is not reachable: {e}"}).encode()

    def _local_dir(self, where: str) -> str:
        d = self.logs_dir if where == "logs" else self.captures_dir
        if not d:
            raise ValueError(f"no {where} folder in this run")
        return d

    def _cap_path(self, rel: str, where: str = "captures") -> str:
        d = self._local_dir(where)
        base = os.path.realpath(d)
        p = os.path.realpath(os.path.join(base, str(rel or "").lstrip("/")))
        if p != base and not p.startswith(base + os.sep):
            raise ValueError(f"that path is outside the {where} folder")
        return p

    def files_list(self, where: str, rel: str) -> Dict[str, Any]:
        if where == "box":
            from urllib.parse import quote
            code, out = self._relay_raw("GET", "/box/files?path=" + quote(rel))
            try:
                return json.loads(out or b"{}")
            except ValueError:
                return {"error": f"no answer from the box ({code})"}
        try:
            os.makedirs(self._local_dir(where), exist_ok=True)
            d = self._cap_path(rel, where)
            if not os.path.isdir(d):
                return {"error": "not a folder"}
            items = []
            for n in os.listdir(d):
                st = os.lstat(os.path.join(d, n))
                kind = "dir" if os.path.isdir(os.path.join(d, n)) else "file"
                items.append({"name": n, "type": kind, "size": st.st_size if kind == "file" else None,
                              "modified": round(st.st_mtime)})
            items.sort(key=lambda e: (e["type"] != "dir", -e["modified"]))
            base = os.path.realpath(self._local_dir(where))
            in_use = os.path.realpath(self.log_in_use) if self.log_in_use else ""
            for e in items:
                e["in_use"] = bool(in_use) and os.path.join(d, e["name"]) == in_use
            return {"path": os.path.relpath(d, base) if d != base else "", "entries": items}
        except (ValueError, OSError) as e:
            return {"error": str(e)}

    def files_read(self, where: str, rel: str):
        """(bytes, None) or (None, error)."""
        if where == "box":
            from urllib.parse import quote
            code, out = self._relay_raw("GET", "/box/files/raw?path=" + quote(rel))
            if code == 200:
                return out, None
            try:
                return None, json.loads(out or b"{}").get("error", f"error {code}")
            except ValueError:
                return None, f"error {code}"
        try:
            p = self._cap_path(rel, where)
            if not os.path.isfile(p):
                return None, "not a file"
            if os.path.getsize(p) > self.FILE_MAX:
                return None, "the file is larger than 25 MB"
            with open(p, "rb") as f:
                return f.read(), None
        except (ValueError, OSError) as e:
            return None, str(e)

    def files_do(self, where: str, op: str, rel: str, name: str = "") -> Dict[str, Any]:
        if where == "box":
            code, out = self._relay_raw("POST", f"/box/files/{op}", json.dumps({"path": rel, "name": name}).encode())
            try:
                r = json.loads(out or b"{}")
            except ValueError:
                r = {"error": f"no answer from the box ({code})"}
        else:
            r = self._captures_do(op, rel, name, where)
        if "message" in r:
            self.record("files", f"{where}: {r['message']}")
        return r

    def _captures_do(self, op: str, rel: str, name: str, where: str = "captures") -> Dict[str, Any]:
        import shutil
        import subprocess
        try:
            if op == "empty-trash":
                return {"error": "these go to the desktop's own Trash; empty it there"}
            if op == "usage":
                return {"error": "disk use is shown for the box only"}
            if op == "mkdir":
                d = self._cap_path(rel, where)
                if not name or "/" in name or name in (".", ".."):
                    return {"error": "not a valid folder name"}
                os.mkdir(os.path.join(d, name))
                return {"ok": True, "message": f"folder {name} made"}
            p = self._cap_path(rel, where)
            if p == os.path.realpath(self._local_dir(where)):
                return {"error": f"the {where} folder itself cannot be changed"}
            if self.log_in_use and p == os.path.realpath(self.log_in_use):
                return {"error": "that is this session's log, still being written; it can go after a restart"}
            if op == "rename":
                if not name or "/" in name or name in (".", ".."):
                    return {"error": "not a valid name"}
                target = os.path.join(os.path.dirname(p), name)
                if os.path.lexists(target):
                    return {"error": f"{name} already exists"}
                os.replace(p, target)
                return {"ok": True, "message": f"renamed to {name}"}
            if op == "trash":                    # the desktop's Trash, as the file manager does; else .trash
                if shutil.which("gio") and subprocess.run(["gio", "trash", p], capture_output=True).returncode == 0:
                    return {"ok": True, "message": f"moved {os.path.basename(p)} to the Trash"}
                t = os.path.join(os.path.realpath(self._local_dir(where)), ".trash")
                os.makedirs(t, exist_ok=True)
                os.replace(p, os.path.join(t, time.strftime("%Y%m%d-%H%M%S-") + os.path.basename(p)))
                return {"ok": True, "message": f"moved {os.path.basename(p)} to {where}/.trash"}
            return {"error": f"unknown operation {op}"}
        except (ValueError, OSError) as e:
            return {"error": str(e)}

    def files_upload(self, rel: str, name: str, data: bytes, replace: bool) -> Dict[str, Any]:
        from urllib.parse import quote
        code, out = self._relay_raw("POST", f"/box/files/upload?path={quote(rel)}&name={quote(name)}"
                                    f"&replace={'1' if replace else '0'}", data, "application/octet-stream")
        try:
            r = json.loads(out or b"{}")
        except ValueError:
            r = {"error": f"no answer from the box ({code})"}
        if "message" in r:
            self.record("files", f"box: {r['message']}")
        return r

    def settings_do(self, op: str, body: dict) -> Dict[str, Any]:
        st = self.settings
        try:
            if op == "set":
                r = st.set(str(body.get("key", "")), str(body.get("value", "")))
            elif op == "clear":
                r = st.clear(str(body.get("key", "")))
            elif op == "args":
                r = st.set_host_args(str(body.get("args", "")))
            elif op == "service":
                r = st.service(str(body.get("name", "")), bool(body.get("on")))
            elif op == "restart":
                code, out = self._relay_raw("GET", "/health")       # never cut a Claude Code job short
                try:
                    running = json.loads(out or b"{}").get("running", 0) if code == 200 else 0
                except ValueError:
                    running = 0
                if running and not body.get("force"):
                    return {"error": "a Claude Code job is running; restart when it has finished", "busy": True}
                r = st.restart()
            else:
                return {"error": f"unknown request {op}"}
        except (ValueError, PermissionError, RuntimeError, OSError) as e:
            r = {"error": str(e)}
        if "message" in r:
            self.record("settings", r["message"])
        elif "error" in r:
            self.record("settings", f"{op} failed: {r['error']}")
        return r

    def _box(self, method: str, path: str, body: Optional[dict] = None):
        """(body, code) for the Box tab: the relay runs box-app in the box."""
        url = self.box_url() if self.box_url else ""
        if not url:
            return {"error": "no Claude Code relay is set (--agent-url), so the box cannot be reached"}, 200
        from .errands import _http
        try:
            r = _http(url.rstrip("/") + path, method, body if method == "POST" else None, timeout=40)
            if method == "POST" and "message" in r:
                self.record("box", r["message"])
            return r, 200
        except RuntimeError as e:
            msg = str(e)
            if " 404 " in f" {msg} " or msg.startswith("404"):
                msg = "the agent at this address has no box (it is not the Claude Code relay)"
            elif msg.startswith(("400", "502")):
                try:
                    msg = json.loads(msg.split(":", 1)[1].split(" from ", 1)[-1].split(": ", 1)[-1]).get("error", msg)
                except (ValueError, IndexError, AttributeError):
                    pass
            return {"error": msg}, 200

    def record(self, kind: str, text: str) -> None:
        self.events.append({"t": time.time(), "kind": kind, "text": str(text)[:400]})

    # lifecycle ----------------------------------------------------------------
    def start(self) -> Optional[str]:
        if self._server is not None:
            return None                        # already serving (started early to show the startup stage)
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep the console for the character
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj: Any, code: int = 200) -> None:
                self._send(code, json.dumps(obj).encode())

            def _send_file(self, data: bytes, name: str, download: bool) -> None:
                """A file from the box or her captures, never as a live page: images as images, text
                (HTML included) as plain text, anything else as a download. The sandbox policy and
                nosniff stop a file Claude Code wrote from running in this page's origin."""
                from urllib.parse import quote
                ext = os.path.splitext(name)[1].lower()
                images = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                          ".webp": "image/webp"}
                if ext in images:
                    ctype = images[ext]
                elif ext in TEXT_EXTS or not ext:
                    ctype = "text/plain; charset=utf-8"
                else:
                    ctype, download = "application/octet-stream", True
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'")
                self.send_header("Content-Disposition", f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quote(name)}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _foreign(self, post: bool) -> bool:
                """True (and 403 sent) for a request another web page made. The page has no password,
                so without this any site open in the browser could post here: a form post, a fetch
                from another origin, or a DNS-rebound name pointing at this machine."""
                host = (self.headers.get("Host") or "").lower()
                name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
                origin = (self.headers.get("Origin") or "").lower()
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                bad = (panel.host in ("127.0.0.1", "localhost", "::1") and name not in ("127.0.0.1", "localhost", "[::1]")) \
                    or (origin and origin != f"http://{host}") \
                    or self.headers.get("Sec-Fetch-Site") == "cross-site" \
                    or (post and ctype not in (("application/json", "application/octet-stream")
                                              if self.path.startswith("/api/files/upload") else ("application/json",)))
                if bad:
                    self._json({"error": "requests from other pages are refused"}, 403)
                return bool(bad)

            def do_GET(self):
                if self._foreign(post=False):
                    return
                u = urlparse(self.path)
                try:
                    if u.path == "/":
                        self._send(200, panel.page().encode(), "text/html; charset=utf-8")
                    elif u.path == "/api/state":
                        self._json(panel.state())
                    elif u.path == "/api/reference":
                        self._json(panel.reference())
                    elif u.path == "/api/snapshot.jpg":
                        jpg = panel.snapshot() if panel.snapshot else None
                        if jpg:
                            self._send(200, jpg, "image/jpeg")
                        else:
                            self._json({"error": "no camera (start with --camera)"}, 404)
                    elif u.path == "/api/wifi":
                        st = panel.wifi.status()
                        if st.get("available") and parse_qs(u.query).get("scan"):
                            st["networks"] = panel.wifi.scan()
                        self._json(st)
                    elif u.path == "/api/dreams":
                        from .dream import list_reports
                        self._json({"reports": list_reports(panel.dreams_dir) if panel.dreams_dir else []})
                    elif u.path.startswith("/api/dreams/"):
                        import os as _os
                        name = _os.path.basename(u.path)
                        path = _os.path.join(panel.dreams_dir or "", name)
                        if not panel.dreams_dir or not name.endswith(".md") or not _os.path.isfile(path):
                            self._json({"error": "no such report"}, 404)
                        else:
                            with open(path, encoding="utf-8") as f:
                                self._send(200, f.read().encode(), "text/plain; charset=utf-8")
                    elif u.path in ("/api/files", "/api/files/raw"):
                        q = parse_qs(u.query)
                        where = {"box": "box", "logs": "logs"}.get((q.get("where") or ["box"])[0], "captures")
                        rel = (q.get("path") or [""])[0]
                        if u.path == "/api/files":
                            self._json(panel.files_list(where, rel))
                        else:
                            data, err = panel.files_read(where, rel)
                            if err:
                                self._json({"error": err}, 404)
                            else:
                                self._send_file(data, os.path.basename(rel), (q.get("download") or ["0"])[0] == "1")
                    elif u.path == "/api/settings":
                        self._json(panel.settings.view())
                    elif u.path == "/api/box":
                        self._json(*panel._box("GET", "/box/apps"))
                    elif u.path == "/api/boards":
                        self._json(panel.boards.boards() if panel.boards else {"error": "no board in use (--satellite)"})
                    elif u.path == "/api/agent":
                        self._json(panel.agent.info() if panel.agent else
                                   {"error": "this face does not hand work to an agent (no \"errands\" in its face.json)"})
                    else:
                        self._json({"error": "not found"}, 404)
                except Exception as e:
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)

            def do_POST(self):
                if self._foreign(post=True):
                    return
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw_body, body = b"", {}
                if u.path == "/api/files/upload":
                    raw_body = self.rfile.read(n) if 0 < n <= panel.FILE_MAX else b""
                else:
                    try:
                        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    except ValueError:
                        body = {}
                try:
                    if u.path.startswith("/api/settings/"):
                        self._json(panel.settings_do(u.path[len("/api/settings/"):], body))
                    elif u.path == "/api/files/upload":
                        q = parse_qs(u.query)
                        if n > panel.FILE_MAX:
                            self._json({"error": "uploads are limited to 25 MB"}, 413)
                        else:
                            self._json(panel.files_upload((q.get("path") or [""])[0], (q.get("name") or [""])[0],
                                                          raw_body, (q.get("replace") or ["0"])[0] == "1"))
                    elif u.path.startswith("/api/files/"):
                        where = {"box": "box", "logs": "logs"}.get(body.get("where", "box"), "captures")
                        self._json(panel.files_do(where, u.path[len("/api/files/"):], str(body.get("path", "")),
                                                  str(body.get("name", ""))))
                    elif u.path.startswith("/api/box/"):
                        self._json(*panel._box("POST", "/box/apps/" + u.path[len("/api/box/"):], body))
                    elif u.path == "/api/boards/reboot":
                        self._json(panel.boards.reboot_board() if panel.boards else {"error": "no board in use (--satellite)"})
                        if panel.boards:
                            panel.record("boards", "Restart board pressed")
                    elif u.path in ("/api/boards/allow", "/api/boards/remove"):
                        if not panel.boards:
                            self._json({"error": "no board in use (--satellite)"})
                        else:
                            fn = panel.boards.allow_board if u.path.endswith("allow") else panel.boards.remove_board
                            err = fn(str(body.get("id", "")))
                            self._json({"error": err} if err else panel.boards.boards())
                    elif u.path == "/api/set":
                        self._json(panel.set(body.get("name", ""), body.get("value")))
                    elif u.path == "/api/action":
                        self._json(panel.run_action(body.get("name", ""), str(body.get("text", ""))))
                    elif u.path == "/api/calibrate":
                        self._json(panel.calibrate(body.get("step", "start")))
                    elif u.path == "/api/wifi/connect":
                        panel.record("wifi", f"joining {body.get('ssid', '')!r}")
                        self._json({"message": panel.wifi.connect(str(body.get("ssid", "")), str(body.get("password", "")))})
                    elif u.path.startswith("/api/agent/") and panel.agent is None:
                        self._json({"error": "this face does not hand work to an agent"})
                    elif u.path == "/api/agent/test":
                        self._json(panel.agent.test(str(body.get("url", ""))))
                    elif u.path == "/api/agent/decide":
                        r = panel.agent.decide(str(body.get("goal", "")), bool(body.get("approve")))
                        panel.record("agent", r.get("message") or r.get("error", ""))
                        self._json(r)
                    elif u.path == "/api/agent/mode":
                        r = panel.agent.set_mode(str(body.get("mode", "")))
                        panel.record("agent", r.get("message") or r.get("error", ""))
                        self._json(r)
                    elif u.path == "/api/agent/set":
                        r = panel.agent.set_url(str(body.get("url", "")), save=bool(body.get("save", True)))
                        if "error" not in r:
                            panel.record("agent", f"address {r['url'] or '(none)'}: {r['message']}")
                        self._json(r)
                    else:
                        self._json({"error": "not found"}, 404)
                except Exception as e:
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        self._server = None
        for port in ([self.port] if self.port == 0 else range(self.port, self.port + 10)):
            try:
                self._server = ThreadingHTTPServer((self.host, port), Handler)
                break
            except OSError as e:
                print(f"[web] port {port} is taken ({e.strerror or e}); trying the next")
        if self._server is None:
            print(f"[web] panel not started: no free port near {self.port}")
            return None
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]          # port 0 = pick a free one (tests)
        self._thread = threading.Thread(target=self._server.serve_forever, name="web-panel", daemon=True)
        self._thread.start()
        local_only = self.host in ("127.0.0.1", "localhost", "::1")
        url = f"http://localhost:{self.port}" if local_only else f"http://{lan_ip()}:{self.port}"
        where = ("this machine only; --web-host 0.0.0.0 opens it to the network" if local_only
                 else "REACHABLE BY ANYONE ON THIS NETWORK, and it has no password")
        print(f"[web] control page at {url}  ({where}; --no-web to disable)")
        name = None if local_only else mdns_name()
        if name:                  # the address that survives a DHCP change; write it on the box
            print(f"[web]   also http://{name}:{self.port} from any phone or laptop on this network")
        return url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # state --------------------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        tun = []
        for t in self.tunables.values():
            try:
                v = t.get()
            except Exception as e:
                v = f"? {e}"
            tun.append({"name": t.name, "value": v, "kind": t.kind, "unit": t.unit, "help": t.help,
                        "lo": t.lo, "hi": t.hi, "flag": t.flag, "options": t.options})
        try:
            status = self.status_fn() or {}
        except Exception as e:
            status = {"error": f"{type(e).__name__}: {e}"}
        return {"status": status, "tunables": tun,
                "actions": [{"name": a.name, "help": a.help, "takes_text": a.takes_text} for a in self.actions.values()],
                "events": list(self.events)[-40:], "calibration": dict(self.calib),
                "has_camera": self.snapshot is not None, "wifi": self.wifi.available,
                "agent": self.agent is not None,
                "uptime_s": round(time.time() - self.started_at)}

    def reference(self) -> Dict[str, Any]:
        flags = describe_parser(self.parser, self.args) if self.parser is not None else []
        face = []
        if self.manifest is not None:
            for k, v in vars(self.manifest).items():
                if k.startswith("_"):
                    continue
                if k == "character":
                    v = (v[:160] + "…") if isinstance(v, str) and len(v) > 160 else v
                face.append({"field": k, "value": json.dumps(v, default=str)[:200], "help": FACE_FIELD_HELP.get(k, "")})
        return {"flags": flags, "face": face}

    def set(self, name: str, raw: Any) -> Dict[str, Any]:
        t = self.tunables.get(name)
        if t is None:
            return {"error": f"unknown tunable {name!r}"}
        try:
            v = t.coerce(raw)
        except ValueError as e:
            return {"error": str(e)}
        t.set(v)
        self.record("panel", f"{name} = {v}")
        return {"name": name, "value": t.get()}

    def run_action(self, name: str, text: str) -> Dict[str, Any]:
        a = self.actions.get(name)
        if a is None:
            return {"error": f"unknown action {name!r}"}
        msg = a.fn(text) or "ok"
        self.record("panel", f"{name}: {msg}"[:200])
        return {"message": msg}

    # calibration --------------------------------------------------------------
    def calibrate(self, step: str) -> Dict[str, Any]:
        if self.calibrator is None:
            return {"error": "calibration is not available in this run"}
        if step == "continue":
            self._calib_go.set()
            return dict(self.calib)
        if self.calib["state"] in ("running", "waiting"):
            return dict(self.calib)

        def ask(msg: str) -> None:
            self.calib.update({"state": "waiting", "prompt": msg.strip()})
            self._calib_go.clear()
            self._calib_go.wait(timeout=600)
            self.calib.update({"state": "running", "prompt": "Talk now, normal voice, 5 seconds"})

        def run() -> None:
            self.calib.update({"state": "running", "prompt": "Measuring the room: stay quiet for 4 s",
                               "result": None, "error": ""})
            self.pause(True)
            try:
                self.calib["result"] = self.calibrator(ask)
                self.calib.update({"state": "done", "prompt": ""})
            except Exception as e:
                self.calib.update({"state": "error", "error": f"{type(e).__name__}: {e}", "prompt": ""})
            finally:
                self.pause(False)
            self.record("calibration", self.calib.get("error") or (self.calib["result"] or {}).get("verdict", "done"))

        threading.Thread(target=run, name="calibration", daemon=True).start()
        return dict(self.calib)

    # page ---------------------------------------------------------------------
    def page(self) -> str:
        return PAGE.replace("{{PORT}}", str(self.port))


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Talker control</title>
<style>
:root{--bg:#f4f2ec;--panel:#fff;--ink:#1b1a1f;--mute:#6a6672;--line:#dbd6cc;--accent:#0e7c8c;--ok:#2e7d4f;--warn:#a8571a;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#0e1015;--panel:#161a22;--ink:#e8e4da;--mute:#9d99a6;--line:#2a2f3a;--accent:#3fc3d6;--ok:#6fd39a;--warn:#f0a16a;--bad:#ff7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 "IBM Plex Sans","Segoe UI",system-ui,sans-serif}
header{display:flex;align-items:baseline;gap:1rem;padding:1rem 1.4rem;border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:1.15rem;margin:0}header .sub{color:var(--mute);font-size:.85rem}
nav{display:flex;gap:.2rem;padding:.4rem 1.4rem;border-bottom:1px solid var(--line);flex-wrap:wrap;background:var(--panel)}
nav button{background:none;border:0;padding:.45rem .8rem;border-radius:6px;color:var(--mute);font:inherit;cursor:pointer}
nav button.on{background:var(--bg);color:var(--ink);font-weight:600}
main{padding:1.2rem 1.4rem;max-width:1100px;margin:0 auto}section{display:none}section.on{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:.8rem}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.8rem 1rem}
.card h3{margin:0 0 .3rem;font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--mute)}
.big{font-size:1.25rem;font-weight:600}.mute{color:var(--mute)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.meter{height:10px;background:var(--bg);border:1px solid var(--line);border-radius:5px;overflow:hidden;margin-top:.4rem}
.meter i{display:block;height:100%;background:var(--accent);width:0;transition:width .12s}
table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--mute)}.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
select,input[type=number],input[type=text],input[type=password]{font:inherit;padding:.3rem .5rem;border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--ink);width:9rem}
a.act,label.act{display:inline-block;text-decoration:none;line-height:normal}
.explorer{display:grid;grid-template-columns:15rem minmax(0,1fr);gap:1.4rem;align-items:start}
@media(max-width:760px){.explorer{grid-template-columns:minmax(0,1fr)}.exside{border-right:0!important;border-bottom:1px solid var(--line);padding-bottom:.6rem}}
.exside{border-right:1px solid var(--line);padding-right:.8rem;position:sticky;top:.5rem}
.exhead{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--mute);margin:1rem 0 .3rem}.exhead:first-child{margin-top:0}
.exitem{display:block;padding:.38rem .55rem;border-radius:6px;cursor:pointer;color:var(--ink);text-decoration:none}
.exitem:hover{background:var(--panel)}.exitem.on{background:var(--panel);font-weight:600;box-shadow:inset 3px 0 0 var(--accent)}
.extree a{display:block;padding:.22rem .55rem;border-radius:5px;font-size:.88rem;color:var(--ink);text-decoration:none;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.extree a:hover{background:var(--panel)}.extree a.on{font-weight:600;color:var(--accent)}
.ico{display:inline-flex;align-items:center;justify-content:center;width:2rem;height:2rem;margin-left:.15rem;border:1px solid transparent;border-radius:6px;background:none;color:var(--mute);font:inherit;font-size:1rem;text-decoration:none;cursor:pointer}
.ico:hover{border-color:var(--line);background:var(--panel);color:var(--ink)}.ico.del:hover{color:var(--bad)}
button.act,a.act,label.act{font:inherit;padding:.4rem .8rem;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);cursor:pointer}
button.act.primary,a.act.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.help{color:var(--mute);font-size:.84rem}.q{display:inline-block;width:1.1em;height:1.1em;border-radius:50%;border:1px solid var(--mute);color:var(--mute);font-size:.7em;text-align:center;line-height:1.1em;margin-left:.3em;cursor:help}
.log{font-family:"IBM Plex Mono",Consolas,monospace;font-size:.82rem;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:.6rem .8rem;max-height:20rem;overflow:auto;white-space:pre-wrap}
.row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin:.5rem 0}code{background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:.05em .35em;font-size:.9em}
h2{font-size:1.05rem;margin:1.2rem 0 .6rem}#toast{position:fixed;bottom:1rem;right:1rem;background:var(--ink);color:var(--bg);padding:.5rem .9rem;border-radius:6px;opacity:0;transition:opacity .2s}
img.snap{max-width:100%;border-radius:8px;border:1px solid var(--line)}
</style></head><body>
<header><h1>Talker control</h1><span class="sub" id="who">connecting…</span><span class="sub" id="up"></span></header>
<nav><button class="on" data-t="status">Status</button><button data-t="tune">Tune</button><button data-t="test">Test setup</button><button data-t="ref">Reference</button><button data-t="wifi">Wi-Fi</button><button data-t="agent">Agent</button><button data-t="boards">Boards</button><button data-t="files">Box Manager</button><button data-t="settings">Settings</button><button data-t="dreams">Dreams</button></nav>
<main>
<section id="status" class="on">
  <div class="grid" id="cards"></div>
  <h2>Last turn</h2><div class="log" id="turn">…</div>
  <h2>Recent events</h2><div class="log" id="events"></div>
</section>
<section id="tune">
  <p class="help">Changes apply at once and last until the program exits. The flag next to each one sets the same thing at startup.</p>
  <div class="wrap"><table id="tunables"><tr><th>setting</th><th>value</th><th>what it does</th></tr></table></div>
</section>
<section id="test">
  <div class="grid">
    <div class="card"><h3>Mic level</h3><div class="big" id="lvl">0</div><div class="meter"><i id="lvlbar"></i></div><div class="help">Aim for 500 to 5,000 while someone talks from the visitor spot. Above 28,000 clips.</div></div>
    <div class="card"><h3>Speaking</h3><div class="big" id="spk">–</div><div class="help">What the speaker is doing right now.</div></div>
  </div>
  <h2>Try things</h2>
  <div id="actions"></div>
  <h2>Camera</h2>
  <div class="row"><button class="act" onclick="snap()">Take a snapshot</button><span class="help" id="camhelp"></span></div>
  <div id="snapbox"></div>
  <h2>Calibrate this room</h2>
  <p class="help">Measures the room, plays the character through the speaker and measures the bleed into the mic, then asks you to talk from the visitor spot. The loop is paused meanwhile. Result is saved to calibration.json and used as the default next start.</p>
  <div class="row"><button class="act primary" onclick="calib('start')">Start calibration</button><button class="act" id="cont" onclick="calib('continue')" disabled>I'm at the visitor spot, continue</button><span id="calmsg" class="help"></span></div>
  <div class="log" id="calres" style="display:none"></div>
</section>
<section id="ref">
  <h2>Command line flags <span class="help">(value in use this run, then the default)</span></h2>
  <div class="wrap"><table id="flags"><tr><th>flag</th><th>in use</th><th>default</th><th>help</th></tr></table></div>
  <h2>face.json fields for this face</h2>
  <div class="wrap"><table id="face"><tr><th>field</th><th>value</th><th>what it does</th></tr></table></div>
</section>
<section id="wifi">
  <p class="help">Through NetworkManager (nmcli), as on Raspberry Pi OS. Joining a network here keeps this page reachable only if the box stays on a network you can also reach.</p>
  <div id="wifistat" class="help"></div>
  <div class="row"><button class="act" onclick="wifi(true)">Scan for networks</button></div>
  <div class="wrap"><table id="nets"><tr><th>network</th><th>signal</th><th>security</th><th></th></tr></table></div>
  <div class="row"><input type="text" id="ssid" placeholder="network name"><input type="password" id="pw" placeholder="password"><button class="act primary" onclick="join()">Join</button><span id="wifimsg" class="help"></span></div>
</section>
<section id="dreams">
  <p class="help">Once a day, after a few idle hours, a strong model reviews her logs and writes proposed improvements. Nothing is applied automatically. "dream now" on Test setup runs one at once.</p>
  <div class="row" id="dreamlist"></div>
  <div class="log" id="dreamtext" style="max-height:none">Pick a report.</div>
</section>
<section id="files">
 <div class="explorer">
  <aside class="exside">
    <div class="exhead">On this computer</div>
    <a class="exitem" id="ex-captures" onclick="flWhere('captures')">📷 Captures</a>
    <a class="exitem" id="ex-logs" onclick="flWhere('logs')">🗒 Logs</a>
    <div class="exhead">Claude Code's box</div>
    <a class="exitem" id="ex-box" onclick="flWhere('box')">📁 Workspace</a>
    <a class="exitem" id="ex-servers" onclick="exServers()">🌐 Servers &amp; library</a>
    <div id="exusage" class="help" style="margin-top:1rem"></div>
    <div id="extree" class="extree"></div>
  </aside>
  <div class="exmain">
   <div id="exfiles">
    <p class="help" id="exhelp"></p>
    <div class="row"><span id="flcrumbs"></span><span style="flex:1"></span><button class="act" onclick="flLoad()">Refresh</button><button class="act" onclick="flMkdir()">New folder</button>
      <label class="act" id="flupl" style="cursor:pointer">Upload…<input type="file" multiple style="display:none" onchange="flUpload(this)"></label>
      <button class="act" id="flempty" onclick="flEmpty()">Empty box trash</button></div>
    <div id="flmsg" class="help"></div>
    <div class="wrap"><table id="fltable"></table></div>
    <div id="flprev" style="margin-top:1rem"></div>
   </div>
   <div id="exservers" style="display:none">
    <p class="help">Servers Claude Code runs in its box, on ports 8100 to 8119 of this machine. Temporary ones stop by themselves after a day; kept ones stay up and come back when the box restarts. Anything on those ports that box-app did not start is stopped within the hour.</p>
    <div id="boxoff" class="help" style="display:none"></div>
  <div id="boxon">
    <div class="row"><span class="help" id="boxset"></span><button class="act" onclick="box()">Refresh</button></div>
    <h2>Servers</h2><div class="wrap"><table id="boxapps"></table></div>
    <h2>Not started by box-app <span class="help">(stopped at the next hourly sweep unless adopted)</span></h2><div class="wrap"><table id="boxstrays"></table></div>
    <h2>Library <span class="help">(everything Claude Code has served; stopping a server keeps its files and its entry here)</span></h2><div class="wrap"><table id="boxlib"></table></div>
  </div>
   </div>
  </div>
 </div>
</section>
<section id="settings">
  <p class="help">Clara's settings, kept in <code>.env</code> in her folder. Keys are never shown: you can replace or clear one, not read it. Most settings are read when she starts, so each says what has to restart.</p>
  <div id="stro" class="help bad" style="display:none"></div>
  <div id="stpending" class="card" style="display:none;margin:.6rem 0"><b class="warn">Saved changes apply after a restart.</b> <button class="act primary" onclick="stRestart()">Restart Clara</button></div>
  <div id="ststatus" class="card" style="display:none;margin:.6rem 0;position:sticky;top:.4rem;z-index:2"></div>
  <div id="stgroups"></div>
  <h2>Add a setting</h2>
  <div class="row"><input type="text" id="stnewk" placeholder="NAME" style="width:16rem;text-transform:uppercase"><input type="text" id="stnewv" placeholder="value" style="width:20rem"><button class="act" onclick="stAdd()">Add</button></div>
  <h2>Her options <span class="help">(the voice host's flags, in <span id="sthostenv"></span>)</span></h2>
  <div class="row"><textarea id="stargs" rows="3" style="width:100%;font-family:'IBM Plex Mono',Consolas,monospace;font-size:.85rem"></textarea></div>
  <div class="row"><button class="act" onclick="stArgs()">Save options</button><span class="help">Every flag is listed on the Reference tab.</span></div>
  <h2>Services</h2>
  <div class="wrap"><table id="stsvc"></table></div>
  <div class="row"><button class="act" onclick="stRestart()">Restart Clara</button><span class="help">Restarts the relay and her voice host; the board reconnects by itself. Not while a Claude Code job runs.</span></div>
</section>
<section id="boards">
  <p class="help">Which boards may connect to this machine as Clara's ears and voice. With nobody on the list, any device on your network can. A board that is not on the list is refused and shows up below, so you can let it in.</p>
  <div id="bdoff" class="help" style="display:none"></div>
  <div id="bdon">
    <div class="grid"><div class="card"><h3>Access</h3><div class="big" id="bdstate">–</div><div class="help" id="bdcur"></div></div></div>
    <div class="row" id="bdlock"></div>
    <div class="row"><button class="act" id="bdreboot" onclick="bdReboot()">Restart board</button><span class="help">If she hears you but you do not hear her, or she stops answering. Works over Wi-Fi; if the board is not connected, unplug it and plug it back in, or press its reset button.</span></div>
    <div id="bdstatus" class="card" style="display:none;margin:.4rem 0"></div>
    <h2>Allowed</h2><div class="wrap"><table id="bdallowed"></table></div>
    <h2>Refused <span class="help">(asked to connect and were turned away)</span></h2><div class="wrap"><table id="bdrefused"></table></div>
  </div>
</section>
<section id="agent">
  <p class="help">Where this character's <code>{{task ...}}</code> errands go: <code>tools/agent_relay.py</code> on the machine with your agent harness (see <code>tools/AGENT_RELAY.md</code>). Changes apply at once; tasks asked for while no address is set wait and are sent when one is. Saved to <code>settings.json</code> for the next start.</p>
  <div id="agentoff" class="help" style="display:none"></div>
  <div id="agenton">
    <div class="grid">
      <div class="card"><h3>Backend</h3><div class="big" id="agstate">–</div><div class="help" id="agsrc"></div></div>
      <div class="card"><h3>Tasks</h3><div class="big" id="agcount">–</div><div class="help">open · done · failed</div></div>
    </div>
    <div class="row"><b>Mode</b><select id="agmode" onchange="agMode()"><option value="auto">auto: she plans, chases and approves what was asked; asks you only for real decisions</option><option value="ask">ask: every action the agent wants to take comes to you</option></select></div>
    <h2>Goals <span class="help">(each request she handed off, its plan and each step's state)</span></h2>
    <div id="aggoals" class="help">none yet</div>
    <h2>Connection</h2>
    <div class="row"><input type="text" id="agurl" style="width:24rem" placeholder="http://agentbox:8030"><button class="act" onclick="agTest()">Test</button><button class="act primary" onclick="agSet()">Save</button><button class="act" onclick="$('#agurl').value='';agSet()">Clear</button><span id="agmsg" class="help"></span></div>
    <h2>What the agent can do <span class="help">(the "errands" list in face.json; she offers these)</span></h2>
    <div class="log" id="agcan"></div>
    <h2>Agent tasks <span class="help">(every single task sent to the agent, newest first)</span></h2>
    <div class="wrap"><table id="agtasks"><tr><th>id</th><th>task</th><th>state</th><th>summary</th></tr></table></div>
  </div>
</section>
</main>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.remove('on'));b.classList.add('on');document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===b.dataset.t));if(b.dataset.t==='ref')loadRef();if(b.dataset.t==='wifi')wifi(false);if(b.dataset.t==='agent')agent(true);if(b.dataset.t==='boards')boards();if(b.dataset.t==='settings')settings();if(b.dataset.t==='files'){if($('#exservers').style.display==='none')flLoad();else box();flUsage()}if(b.dataset.t==='dreams')dreams();});
async function dreams(){const r=await (await fetch('/api/dreams')).json();$('#dreamlist').innerHTML=(r.reports||[]).length?r.reports.map(n=>`<button class="act" onclick="dreamOpen('${esc(n)}')">${esc(n.replace('.md',''))}</button>`).join(''):'<span class="help">No dreams yet.</span>';if((r.reports||[]).length)dreamOpen(r.reports[0])}
async function dreamOpen(n){const r=await fetch('/api/dreams/'+encodeURIComponent(n));$('#dreamtext').textContent=r.ok?await r.text():'could not load '+n}
function toast(m){const t=$('#toast');t.textContent=m;t.style.opacity=1;clearTimeout(t._h);t._h=setTimeout(()=>t.style.opacity=0,2200)}
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json()}
let built=false;
function buildTunables(list){const t=$('#tunables');list.forEach(x=>{const tr=document.createElement('tr');const ctl=x.options?`<select data-n="${x.name}">${x.options.map(o=>`<option ${o===x.value?'selected':''}>${esc(o)}</option>`).join('')}</select>`:x.kind==='bool'?`<input type="checkbox" data-n="${x.name}" ${x.value?'checked':''}>`:`<input type="${x.kind==='str'?'text':'number'}" data-n="${x.name}" value="${esc(x.value)}" ${x.lo!=null?`min="${x.lo}"`:''} ${x.hi!=null?`max="${x.hi}"`:''} step="${x.kind==='int'?1:'any'}"> <span class="mute">${esc(x.unit)}</span>`;tr.innerHTML=`<td><b>${esc(x.name)}</b>${x.flag?`<div class="help"><code>${esc(x.flag)}</code></div>`:''}</td><td>${ctl}</td><td class="help">${esc(x.help)}</td>`;t.appendChild(tr);});
 t.querySelectorAll('input,select').forEach(i=>i.onchange=async()=>{const v=i.type==='checkbox'?i.checked:i.value;const r=await post('/api/set',{name:i.dataset.n,value:v});toast(r.error||`${i.dataset.n} = ${r.value}`)});built=true}
function buildActions(list){const d=$('#actions');d.innerHTML='';list.forEach(a=>{const row=document.createElement('div');row.className='row';row.innerHTML=`<button class="act">${esc(a.name)}</button>${a.takes_text?`<input type="text" style="width:22rem" placeholder="text">`:''}<span class="help">${esc(a.help)}</span>`;row.querySelector('button').onclick=async()=>{const inp=row.querySelector('input');const r=await post('/api/action',{name:a.name,text:inp?inp.value:''});toast(r.error||r.message)};d.appendChild(row)})}
function card(h,v,cls,help){return `<div class="card"><h3>${esc(h)}</h3><div class="big ${cls||''}">${esc(v)}</div>${help?`<div class="help">${esc(help)}</div>`:''}</div>`}
async function tick(){try{const s=await (await fetch('/api/state')).json();const st=s.status||{};$('#who').textContent=`${st.face||'?'} · ears ${st.stt||'-'} · brain ${st.llm||'-'} · voice ${st.tts||'-'}`;$('#up').textContent=`up ${Math.floor(s.uptime_s/60)} min`;
 const mode=st.starting?'starting':(st.waiting?'waiting':(st.engaged===false?'dormant':(st.thinking?'thinking':(st.speaking?'speaking':'listening'))));
 if(st.starting){$('#cards').innerHTML=card('State','starting','warn',`${st.starting} (${st.starting_s||0} s so far; about a minute in all on a Pi Zero)`);$('#turn').textContent='still starting';return}
 $('#cards').innerHTML=card('State',mode,mode==='speaking'?'ok':(mode==='dormant'?'warn':''),st.mode_help||'')+card('Mic level',Math.round(st.mic_rms||0),'', 'live, from the microphone')+card('Time to first audio',st.first_audio_ms!=null?st.first_audio_ms+' ms':'–','','last reply, from the moment the text was ready')+card('Heard',st.last_heard||'–','','')+card('Said',st.last_said||'–','','')+(st.vision?card('Vision',`${st.vision.described} looks, ${st.vision.skipped} skipped`,'',st.vision.latest||''):'')
 +(st.brain?card('Brain',st.brain.ok?'answering':('failing: '+(st.brain.kind||'error').replace(/_/g,' ')),st.brain.ok?'ok':'bad',st.brain.ok?'':'since '+(st.brain.since||'')):'')
 +(st.online!==undefined?card('Internet',st.online?'online':'offline',st.online?'ok':'bad',`${st.outages||0} outages this run`):'');
 $('#turn').textContent=st.last_turn||'no turn yet';
 $('#events').innerHTML=(s.events||[]).slice().reverse().map(e=>`<div><span class="mute">${new Date(e.t*1000).toLocaleTimeString()}</span> [${esc(e.kind)}] ${esc(e.text)}</div>`).join('');
 $('#lvl').textContent=Math.round(st.mic_rms||0);$('#lvlbar').style.width=Math.min(100,Math.log10(1+(st.mic_rms||0))/4.5*100)+'%';$('#spk').textContent=st.speaking?'speaking':'quiet';
 if(!built&&s.tunables.length){buildTunables(s.tunables);buildActions(s.actions);$('#camhelp').textContent=s.has_camera?'':'no camera in this run (start with --camera, or --satellite-camera for the board)';}
 const c=s.calibration||{};$('#cont').disabled=c.state!=='waiting';$('#calmsg').textContent=c.state==='idle'?'':`${c.state}: ${c.prompt||c.error||''}`;if(c.result){const r=c.result;$('#calres').style.display='block';$('#calres').textContent=`ambient ${r.ambient}  speaker bleed ${r.speaker}  person ${r.person}  (person is ${r.person_over_speaker}x the speaker)\n${r.verdict}\nrecommended --barge-in-boost ${r.barge_in_boost}${r.mic_gain!=='ok'?'\nmic gain: '+r.mic_gain:''}`}
}catch(e){$('#who').textContent='not reachable: '+e}}
async function loadRef(){const r=await (await fetch('/api/reference')).json();$('#flags').innerHTML='<tr><th>flag</th><th>in use</th><th>default</th><th>help</th></tr>'+r.flags.map(f=>`<tr><td><code>${esc(f.flags)}</code></td><td>${esc(f.value)}</td><td class="mute">${esc(f.default)}</td><td class="help">${esc(f.help)}</td></tr>`).join('');$('#face').innerHTML='<tr><th>field</th><th>value</th><th>what it does</th></tr>'+r.face.map(f=>`<tr><td><code>${esc(f.field)}</code></td><td><code>${esc(f.value)}</code></td><td class="help">${esc(f.help)}</td></tr>`).join('')}
async function snap(){const b=$('#snapbox');b.innerHTML='<span class="help">taking…</span>';const r=await fetch('/api/snapshot.jpg?'+Date.now());if(!r.ok){b.innerHTML='<span class="help">'+esc((await r.json()).error)+'</span>';return}const u=URL.createObjectURL(await r.blob());b.innerHTML=`<img class="snap" src="${u}">`}
async function calib(step){const r=await post('/api/calibrate',{step});if(r.error)toast(r.error)}
async function wifi(scan){const r=await (await fetch('/api/wifi'+(scan?'?scan=1':''))).json();if(!r.available){$('#wifistat').textContent=r.reason;return}$('#wifistat').innerHTML=(r.devices||[]).map(d=>`${esc(d.device)} (${esc(d.type)}): <b>${esc(d.state)}</b> ${esc(d.connection)}`).join(' · ')+` · this box is ${esc(r.ip)}`;if(r.networks){$('#nets').innerHTML='<tr><th>network</th><th>signal</th><th>security</th><th></th></tr>'+r.networks.map(n=>`<tr><td>${n.active?'<b>':''}${esc(n.ssid)}${n.active?'</b> (connected)':''}</td><td>${n.signal}%</td><td>${esc(n.security)}</td><td><button class="act" onclick="$('#ssid').value=${JSON.stringify(n.ssid)}">use</button></td></tr>`).join('')}}
async function join(){$('#wifimsg').textContent='joining…';const r=await post('/api/wifi/connect',{ssid:$('#ssid').value,password:$('#pw').value});$('#wifimsg').textContent=r.error||r.message;wifi(false)}
const SRC={flag:'from --agent-url',settings:'from settings.json',env:'from AGENT_RELAY_URL in .env',none:'not set at startup'};
async function agent(fill){const r=await (await fetch('/api/agent')).json();if(r.error){$('#agenton').style.display='none';$('#agentoff').style.display='block';$('#agentoff').textContent=r.error;return}
 $('#agenton').style.display='block';$('#agentoff').style.display='none';if(fill)$('#agurl').value=r.url||'';
 $('#agstate').textContent=!r.url?'no address':(r.reachable===true?'reachable':(r.reachable===false?'unreachable':'not tried yet'));$('#agstate').className='big '+(!r.url?'warn':(r.reachable===true?'ok':(r.reachable===false?'bad':'')));
 $('#agsrc').textContent=(r.url||'')+(r.url?' · ':'')+(SRC[r.source]||'')+` · polled every ${r.poll_s}s`;$('#agcount').textContent=`${r.open} · ${r.stats.done} · ${r.stats.failed}`;$('#agcan').textContent=r.can||'(not listed; add what the agent can do under "errands" in face.json)';
 if(r.mode&&document.activeElement!==$('#agmode'))$('#agmode').value=r.mode;$('#aggoals').innerHTML=(r.goals||[]).length?r.goals.map(goalHtml).join(''):'none yet';
 $('#agtasks').innerHTML='<tr><th>id</th><th>task</th><th>state</th><th>summary</th></tr>'+(r.tasks||[]).map(t=>`<tr><td><code>${esc(t.id)}</code></td><td>${esc(t.task)}</td><td class="${t.state==='done'?'ok':(t.state==='failed'?'bad':'')}">${esc(t.state)}</td><td class="help" title="${esc(t.result)}">${esc(t.summary)}</td></tr>`).join('')}
function goalHtml(g){const cls=g.state==='done'?'ok':(g.state==='failed'?'bad':(g.state==='needs_input'?'warn':''));const ask=g.state==='needs_input'?`<div class="row"><b class="warn">Needs your decision:</b> <button class="act primary" onclick="agDecide('${g.id}',true)">Approve</button><button class="act" onclick="agDecide('${g.id}',false)">Deny</button></div>`:'';
 return `<div class="card" style="margin:.5rem 0"><h3>${esc(g.id)} · <span class="${cls}">${esc(g.state)}</span></h3><div><b>${esc(g.text)}</b></div>${ask}<table>${(g.steps||[]).map(s=>`<tr><td>${s.n}${s.after.length?' <span class="mute">after '+s.after.join(',')+'</span>':''}</td><td>${esc(s.do)}${s.kind==='action'?` <span class="mute">[action${s.approved?', approved':''}]</span>`:''}${s.question&&s.state==='needs_input'?`<div class="log">${esc(s.question)}</div>`:''}</td><td class="${s.state==='done'?'ok':(s.state==='failed'?'bad':(s.state==='needs_input'?'warn':''))}">${esc(s.state)}${s.tries>1?' ×'+s.tries:''}</td><td class="help">${esc(s.summary)}</td></tr>`).join('')}</table></div>`}
async function agDecide(g,ok){const r=await post('/api/agent/decide',{goal:g,approve:ok});toast(r.error||r.message);agent(false)}
async function agMode(){const r=await post('/api/agent/mode',{mode:$('#agmode').value});toast(r.error||r.message)}
async function agTest(){$('#agmsg').textContent='testing…';const r=await post('/api/agent/test',{url:$('#agurl').value});$('#agmsg').textContent=r.error||(r.ok?`reachable in ${r.ms} ms${r.running!=null?', '+r.running+' running':''}`:`not reachable: ${r.detail}`)}
async function agSet(){const r=await post('/api/agent/set',{url:$('#agurl').value,save:true});$('#agmsg').textContent=r.error||r.message;agent(false)}
async function box(){const r=await (await fetch('/api/box')).json();
 if(r.error){$('#boxon').style.display='none';$('#boxoff').style.display='block';$('#boxoff').textContent=r.error;return}
 $('#boxon').style.display='block';$('#boxoff').style.display='none';const st=r.settings||{};
 $('#boxset').textContent=`temporary servers stop after ${st.ttl_hours} h; at most ${st.max_temporary} at once`;
 const life=a=>a.keep?'<span class="ok">kept</span>':`temporary, ${Math.max(0,a.expires_in_h)} h left`;
 $('#boxapps').innerHTML='<tr><th>name</th><th>address</th><th>state</th><th>lifetime</th><th>folder</th><th></th></tr>'+((r.apps||[]).length?r.apps.map(a=>`<tr><td><b>${esc(a.name)}</b></td><td><a href="http://localhost:${a.port}" target="_blank" rel="noopener">localhost:${a.port}</a></td><td class="${a.running?'ok':'bad'}">${a.running?'running':'not running'}</td><td>${life(a)}</td><td class="help">${esc(a.dir)}</td><td style="white-space:nowrap">${a.keep?`<button class="act" onclick="boxDo('unkeep',{name:'${esc(a.name)}'})">Make temporary</button>`:`<button class="act" onclick="boxDo('keep',{name:'${esc(a.name)}'})">Keep</button>`} <button class="act" onclick="boxStop('${esc(a.name)}')">Stop</button></td></tr>`).join(''):'<tr><td class="help" colspan="6">nothing running</td></tr>');
 $('#boxstrays').innerHTML='<tr><th>address</th><th>command</th><th></th></tr>'+((r.strays||[]).length?r.strays.map(s=>`<tr><td><a href="http://localhost:${s.port}" target="_blank" rel="noopener">localhost:${s.port}</a></td><td class="help"><code>${esc(s.cmd)}</code></td><td style="white-space:nowrap"><button class="act" onclick="boxAdopt(${s.port})">Adopt</button> <button class="act" onclick="boxDo('stop-port',{port:${s.port}})">Stop</button></td></tr>`).join(''):'<tr><td class="help" colspan="3">none</td></tr>');boxLib(r)}
const FL={where:'box',path:''};
function flSize(n){return n==null?'':n<1024?n+' B':n<1048576?(n/1024).toFixed(0)+' KB':(n/1048576).toFixed(1)+' MB'}
function flJoin(a,b){return a?a+'/'+b:b}
function flRaw(p,dl){return `/api/files/raw?where=${FL.where}&path=${encodeURIComponent(p)}${dl?'&download=1':''}&t=${Date.now()}`}
const EXHELP={captures:'Her photos and scans, each with what she saw in it. Deleting moves them to the desktop Trash.',
 logs:'Her session logs: what was said, so treat them as private. The log of the session running now cannot be deleted until a restart.',
 box:'Everything in Claude Code\'s box (~/work). Files open as pictures or plain text; pages from the box are shown as their source, never run. Deleting moves them to the box trash.'};
function exMark(id){document.querySelectorAll('.exitem').forEach(x=>x.classList.toggle('on',x.id===id))}
function exServers(){$('#exfiles').style.display='none';$('#exservers').style.display='';exMark('ex-servers');$('#extree').innerHTML='';box()}
function flWhere(w){FL.where=w;FL.path='';$('#flprev').innerHTML='';$('#exfiles').style.display='';$('#exservers').style.display='none';flLoad()}
function exTree(dirs){const t=$('#extree');const item=$('#ex-'+FL.where);item.after(t);
 const parts=FL.path?FL.path.split('/'):[];let acc='',html='';
 parts.forEach((p,i)=>{acc=flJoin(acc,p);const a=acc;html+=`<a style="padding-left:${.55+(i+1)*.8}rem" class="${a===FL.path?'on':''}" onclick="flGo('${esc(a).replace(/'/g,"\\'")}')">📂 ${esc(p)}</a>`});
 html+=dirs.map(d=>{const a=flJoin(FL.path,d);return `<a style="padding-left:${.55+(parts.length+1)*.8}rem" onclick="flGo('${esc(a).replace(/'/g,"\\'")}')">📁 ${esc(d)}</a>`}).join('');t.innerHTML=html}
function flGo(p){FL.path=p;$('#flprev').innerHTML='';flLoad()}
async function flLoad(){exMark('ex-'+FL.where);$('#exhelp').textContent=EXHELP[FL.where]||'';
 $('#flupl').style.display=$('#flempty').style.display=FL.where==='box'?'':'none';$('#flmsg').textContent='loading…';
 const r=await (await fetch(`/api/files?where=${FL.where}&path=${encodeURIComponent(FL.path)}`)).json();
 if(r.error){$('#flmsg').textContent=r.error;$('#fltable').innerHTML='';return}
 $('#flmsg').textContent=r.entries.length?'':'empty';FL.path=r.path||'';exTree(r.entries.filter(e=>e.type==='dir').map(e=>e.name));
 const parts=FL.path?FL.path.split('/'):[];let acc='';
 $('#flcrumbs').innerHTML=`<a href="#" onclick="FL.path='';flLoad();return false">${FL.where==='box'?'~/work':FL.where}</a>`+parts.map(p=>{acc=flJoin(acc,p);const a=acc;return ` / <a href="#" onclick="FL.path='${esc(a)}';flLoad();return false">${esc(p)}</a>`}).join('');
 $('#fltable').innerHTML='<tr><th>name</th><th>size</th><th>modified</th><th></th></tr>'+r.entries.map(e=>{const p=flJoin(FL.path,e.name);const ep=esc(p).replace(/'/g,"\\'");
  const open=e.type==='dir'?`<a href="#" onclick="FL.path='${ep}';$('#flprev').innerHTML='';flLoad();return false">📁 ${esc(e.name)}</a>`:`<a href="#" onclick="flShow('${ep}');return false">${esc(e.name)}</a>`;
  return `<tr><td>${open}${e.type==='link'?' <span class="help">(link)</span>':''}${e.in_use?' <span class="help">(this session, still being written)</span>':''}</td><td>${flSize(e.size)}</td><td class="help">${new Date(e.modified*1000).toLocaleString()}</td><td style="white-space:nowrap">${e.type==='file'?`<a class="ico" href="${flRaw(p,true)}" title="Download" aria-label="Download ${esc(e.name)}">⬇</a>`:''}<button class="ico" title="Rename" aria-label="Rename ${esc(e.name)}" onclick="flRename('${ep}','${esc(e.name).replace(/'/g,"\\'")}')">✎</button><button class="ico del" title="Delete" aria-label="Delete ${esc(e.name)}" onclick="flTrash('${ep}')">🗑</button></td></tr>`}).join('')}
async function flUsage(){const u=await post('/api/files/usage',{where:'box'});if(u.error){$('#exusage').textContent='';return}
 const g=n=>(n/1073741824).toFixed(1)+' GB';const used=u.disk_total-u.disk_free;const pct=Math.round(100*used/u.disk_total);
 $('#exusage').innerHTML=`<div class="exhead">Box disk</div><b class="${pct>85?'bad':pct>70?'warn':''}">${g(used)} of ${g(u.disk_total)} (${pct}%)</b><br>workspace ${flSize(u.work)}<br>box trash ${flSize(u.trash)}`+(u.host_vm!=null?`<br>on this computer: ${g(u.host_vm)}, ${g(u.host_free)} free`:'')}
async function flShow(p){const b=$('#flprev');const ext=(p.split('.').pop()||'').toLowerCase();
 if(['png','jpg','jpeg','gif','webp'].includes(ext)){b.innerHTML=`<h2>${esc(p)}</h2><img class="snap" src="${flRaw(p)}">`;return}
 const r=await fetch(flRaw(p));if(!r.ok){b.innerHTML='<span class="help">'+esc((await r.json()).error||'could not open it')+'</span>';return}
 const ct=r.headers.get('Content-Type')||'';if(!ct.startsWith('text/')){b.innerHTML=`<h2>${esc(p)}</h2><span class="help">No preview for this kind of file. </span><a class="act" href="${flRaw(p,true)}">Download</a>`;return}
 let t=await r.text();const cut=t.length>300000;if(cut)t=t.slice(0,300000);b.innerHTML=`<h2>${esc(p)}</h2>`;const pre=document.createElement('div');pre.className='log';pre.style.maxHeight='40rem';pre.textContent=t+(cut?'\n… (cut; download for the rest)':'');b.appendChild(pre)}
async function flTrash(p){setTimeout(flUsage,800);if(!confirm('Delete '+p+'? It goes to '+(FL.where==='box'?'the box trash (~/work/.trash)':'the desktop Trash')+'.'))return;const r=await post('/api/files/trash',{where:FL.where,path:p});toast(r.error||r.message);$('#flprev').innerHTML='';flLoad()}
async function flRename(p,old){const n=prompt('New name for '+old+':',old);if(!n||n===old)return;const r=await post('/api/files/rename',{where:FL.where,path:p,name:n});toast(r.error||r.message);flLoad()}
async function flMkdir(){const n=prompt('Name of the new folder:');if(!n)return;const r=await post('/api/files/mkdir',{where:FL.where,path:FL.path,name:n});toast(r.error||r.message);flLoad()}
async function flEmpty(){if(!confirm('Empty the box trash? Those files are deleted for good.'))return;const r=await post('/api/files/empty-trash',{where:'box'});toast(r.error||r.message);flLoad()}
async function flUpload(inp){for(const f of inp.files){$('#flmsg').textContent='uploading '+f.name+'…';
 let r=await (await fetch(`/api/files/upload?path=${encodeURIComponent(FL.path)}&name=${encodeURIComponent(f.name)}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f})).json();
 if(r.error&&/already exists/.test(r.error)&&confirm(f.name+' is already there. Replace it?'))r=await (await fetch(`/api/files/upload?path=${encodeURIComponent(FL.path)}&name=${encodeURIComponent(f.name)}&replace=1`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f})).json();
 toast(r.error||r.message)}inp.value='';flLoad()}
const APPLIES={host:'restart Clara',relay:'restart Clara (the relay)',both:'restart Clara'};
async function settings(){const r=await (await fetch('/api/settings')).json();ST=r;
 $('#stro').style.display=r.writable?'none':'block';$('#stro').textContent=r.writable?'':'Read-only: the control page is open to the network (--web-host 0.0.0.0), so settings cannot be changed from here.';
 $('#stpending').style.display=r.pending.length?'block':'none';$('#sthostenv').textContent=r.host_env;
 if(document.activeElement!==$('#stargs'))$('#stargs').value=r.host_args;
 const groups={};r.settings.forEach(x=>(groups[x.group]=groups[x.group]||[]).push(x));
 $('#stgroups').innerHTML=Object.entries(groups).map(([g,rows])=>`<h2>${esc(g)}</h2><div class="wrap"><table>${rows.map(stRow).join('')}</table></div>`).join('');
 $('#stsvc').innerHTML='<tr><th>service</th><th>state</th><th>at start-up</th><th></th></tr>'+(r.services.length?r.services.map(v=>`<tr><td><code>${esc(v.name)}</code></td><td class="${v.active==='active'?'ok':'bad'}">${esc(v.active)}</td><td>${esc(v.enabled)}</td><td>${v.name==='clara-speaker'?`<button class="act" onclick="stSvc('${v.name}',${v.enabled!=='enabled'})">${v.enabled==='enabled'?'Turn off':'Turn on'}</button> <span class="help">plays a USB board's speech on this computer</span>`:''}</td></tr>`).join(''):'<tr><td class="help" colspan="4">not installed as services (services/install.sh)</td></tr>')}
let ST=null;
function stRow(x){const id='st_'+x.key;const badge=`<span class="help">${esc(APPLIES[x.applies]||'restart Clara')}</span>`;
 const input=x.kind==='secret'?`<span class="${x.set?'ok':'help'}">${x.set?'set ✓':'not set'}</span>${x.alias?` <span class="help">(as ${esc(x.alias)})</span>`:''} <input type="password" id="${id}" placeholder="${x.set?'replace…':'paste it here'}" autocomplete="off" style="width:16rem">`
  :`<input type="${x.kind==='number'?'number':'text'}" ${x.kind==='number'?'step="0.01" min="0"':''} id="${id}" value="${esc(x.value||'')}" style="width:20rem">`;
 return `<tr><td style="width:15rem"><b>${esc(x.label)}</b><div class="help"><code>${esc(x.key)}</code></div></td><td>${input}<div class="help">${esc(x.help)}</div></td><td style="white-space:nowrap"><button class="act" onclick="stSet('${x.key}')">Save</button>${x.set?` <button class="act" onclick="stClear('${x.key}')">Clear</button>`:''}<div>${badge}</div></td></tr>`}
async function stSet(k){const el=$('#st_'+k);const r=await post('/api/settings/set',{key:k,value:el.value});toast(r.error||r.message);if(!r.error&&el.type==='password')el.value='';settings()}
async function stClear(k){if(!confirm('Remove '+k+' from .env?'))return;const r=await post('/api/settings/clear',{key:k});toast(r.error||r.message);settings()}
async function stAdd(){const k=$('#stnewk').value.trim().toUpperCase();const r=await post('/api/settings/set',{key:k,value:$('#stnewv').value});toast(r.error||r.message);if(!r.error){$('#stnewk').value='';$('#stnewv').value=''}settings()}
async function stArgs(){const r=await post('/api/settings/args',{args:$('#stargs').value});toast(r.error||r.message);settings()}
async function stSvc(n,on){const r=await post('/api/settings/service',{name:n,on});toast(r.error||r.message);settings()}
function stStatus(cls,html){const b=$('#ststatus');b.style.display='block';b.innerHTML=`<span class="${cls}">${html}</span>`}
let ST_RESTARTING=false;
async function stRestart(){if(ST_RESTARTING)return;if(!confirm('Restart Clara now? She is away for a few seconds.'))return;
 let r;try{r=await post('/api/settings/restart',{})}catch(e){r={error:'the control page did not answer: '+e}}
 if(r.busy){if(!confirm(r.error+'. Restart anyway and cut it short?')){stStatus('warn','Not restarted: '+esc(r.error)+'.');return}
  r=await post('/api/settings/restart',{force:true})}
 if(r.error){stStatus('bad','<b>Not restarted.</b> '+esc(r.error));return}
 ST_RESTARTING=true;$('#stpending').style.display='none';
 const want=r.services,asked=r.asked,t0=Date.now();stStatus('warn','<b>Restarting Clara…</b> '+want.join(' and ')+' are stopping and starting again.');
 const tick=async()=>{let d=null;try{d=await (await fetch('/api/settings',{cache:'no-store'})).json()}catch(e){}
  const secs=Math.round((Date.now()-t0)/1000);
  if(d){const back=want.every(n=>{const v=d.services.find(x=>x.name===n);return v&&v.active==='active'&&v.since&&v.since>=asked-1});
   if(back){ST_RESTARTING=false;stStatus('ok','<b>Clara is back</b>, restarted at '+new Date().toLocaleTimeString()+' ('+secs+' s). Saved settings are in use; the board reconnects when she is next woken.');settings();return}}
  if(secs>60){ST_RESTARTING=false;stStatus('bad','<b>Clara did not come back within a minute.</b> Check <code>systemctl --user status clara-host clara-relay</code> on this computer.');return}
  stStatus('warn','<b>Restarting Clara…</b> '+(d?'waiting for the services to start':'the page is reconnecting')+' ('+secs+' s)');setTimeout(tick,1000)};
 setTimeout(tick,1500)}
function boxLib(r){const day=t=>t?new Date(t*1000).toLocaleDateString():'';
 $('#boxlib').innerHTML='<tr><th>name</th><th>what it is</th><th>last run</th><th>runs</th><th>folder</th><th></th></tr>'+((r.library||[]).length?r.library.map(e=>`<tr><td><b>${esc(e.name)}</b></td><td>${esc(e.about||'')}</td><td>${day(e.last_started)}${e.running?' <span class="ok">running</span>':(e.why?'<div class="help">'+esc(e.why)+'</div>':'')}</td><td>${e.starts}</td><td class="help">${esc(e.dir)}${e.dir_exists?'':' <span class="bad">(gone)</span>'}</td><td style="white-space:nowrap">${e.running?'':`${e.dir_exists?`<button class="act" onclick="boxDo('restart',{name:'${esc(e.name)}'})">Start again</button> `:''}<button class="act" onclick="boxForget('${esc(e.name)}')">Forget</button>`}</td></tr>`).join(''):'<tr><td class="help" colspan="6">empty</td></tr>')}
async function boxForget(n){if(confirm('Remove '+n+' from the library? Its files in the box are not deleted.'))boxDo('forget',{name:n})}
async function boxDo(a,b){const r=await post('/api/box/'+a,b);toast(r.error||r.message);box()}
async function boxStop(n){if(confirm('Stop '+n+'? It is removed from the list; Claude Code can start it again.'))boxDo('stop',{name:n})}
async function boxAdopt(p){const n=prompt('A name for the server on port '+p+' (lowercase letters, digits, dashes):');if(n)boxDo('adopt',{name:n.trim().toLowerCase(),port:p})}
setInterval(()=>{if($('#files').classList.contains('on')&&$('#exservers').style.display!=='none')box()},10000);
async function boards(){const r=await (await fetch('/api/boards')).json();
 if(r.error){$('#bdon').style.display='none';$('#bdoff').style.display='block';$('#bdoff').textContent=r.error;return}
 $('#bdon').style.display='block';$('#bdoff').style.display='none';const c=r.current||{};
 $('#bdstate').textContent=r.open?'open to any device':'allow-list on';$('#bdstate').className='big '+(r.open?'warn':'ok');
 $('#bdcur').textContent=c.id?`board in use${c.board?': '+c.board:''}${c.ip?' at '+c.ip:''}, ${c.connected?'connected':'not connected now'}`:'no board has connected since the start';
 $('#bdreboot').disabled=!(c.connected);
 $('#bdlock').innerHTML=r.open&&c.id?`<button class="act primary" onclick="bdAllow('${esc(c.id)}')">Allow only this board</button><span class="help">every other device is refused from then on</span>`:'';
 $('#bdallowed').innerHTML='<tr><th>board id</th><th></th><th></th></tr>'+(r.allowed.length?r.allowed.map(a=>`<tr><td><code>${esc(a.id)}</code></td><td class="help">${a.current?'the board in use':''}</td><td>${a.locked?'<span class="help">set in .env</span>':`<button class="act" onclick="bdRemove('${esc(a.id)}')">Remove</button>`}</td></tr>`).join(''):'<tr><td class="help" colspan="3">none: any device may connect</td></tr>');
 $('#bdrefused').innerHTML='<tr><th>board id</th><th>address</th><th>last tried</th><th>tries</th><th></th></tr>'+(r.refused.length?r.refused.map(d=>`<tr><td><code>${esc(d.id)}</code>${d.mac&&d.client?'<div class="help">MAC '+esc(d.mac)+'</div>':''}</td><td>${esc(d.ip)}</td><td>${new Date(d.seen*1000).toLocaleTimeString()}</td><td>${d.count}</td><td><button class="act primary" onclick="bdAllow('${esc(d.id)}')">Allow</button></td></tr>`).join(''):'<tr><td class="help" colspan="5">none</td></tr>')}
let BD_BOOT=false;
async function bdReboot(){if(BD_BOOT)return;if(!confirm('Restart the board? It is away for about half a minute.'))return;
 const st=$('#bdstatus');const show=(cls,h)=>{st.style.display='block';st.innerHTML=`<span class="${cls}">${h}</span>`};
 const r=await post('/api/boards/reboot',{});if(r.error){show('bad','<b>Not restarted.</b> '+esc(r.error));return}
 BD_BOOT=true;const asked=r.asked,t0=Date.now();show('warn','<b>Restarting the board…</b>');
 const tick=async()=>{let d=null;try{d=await (await fetch('/api/boards',{cache:'no-store'})).json()}catch(e){}
  const secs=Math.round((Date.now()-t0)/1000);
  if(d&&d.current&&d.current.connected&&d.connected_at>asked){BD_BOOT=false;show('ok','<b>The board is back</b> ('+secs+' s). If you still do not hear her, unplug it and plug it back in.');boards();return}
  if(secs>120){BD_BOOT=false;show('bad','<b>The board has not come back in two minutes.</b> Unplug it and plug it back in, or press its reset button. It rejoins Wi-Fi and checks in by itself.');return}
  show('warn','<b>Restarting the board…</b> '+(secs<8?'it is rebooting':'waiting for it to rejoin Wi-Fi and check in')+' ('+secs+' s)');setTimeout(tick,1500)};
 setTimeout(tick,2000)}
async function bdAllow(id){if(!confirm('Let board '+id+' connect from now on? Only do this for a board you own.'))return;const r=await post('/api/boards/allow',{id});toast(r.error||'allowed');boards()}
async function bdRemove(id){const r=await post('/api/boards/remove',{id});toast(r.error||'removed');boards()}
setInterval(()=>{if($('#boards').classList.contains('on'))boards()},3000);
setInterval(()=>{if($('#agent').classList.contains('on'))agent(false)},3000);
tick();setInterval(tick,500);
</script></body></html>
"""
