"""
talker/settings_store.py: the control page's Settings tab. Reads and writes the repo's .env,
her voice host options (~/.config/clara-code/host.env) and her user services, without ever
handing a secret back to the page.

  * Secrets (API keys, tokens): the page sees only whether one is set. It can replace one or
    clear it; it can never read one back.
  * Every write keeps the file's other lines and comments, is atomic, and leaves .env at 0600.
  * Writes are refused when the control page is open to the network (--web-host 0.0.0.0):
    someone on the LAN could otherwise point her jobs elsewhere or swap her keys.
  * Most settings are read when a program starts, so each one says what has to restart.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .env_config import HERE

ENV_PATH = os.path.join(HERE, ".env")
HOST_ENV = os.path.expanduser("~/.config/clara-code/host.env")
SERVICES = ("clara-relay", "clara-host", "clara-speaker")
KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS)$|_KEY_|API_KEY")

# Known settings: (key, group, label, help, kind, applies). kind: secret | text | number.
# applies: "host" (restart Clara), "relay" (restart the relay), "both".
KNOWN: List[tuple] = [
    ("ANTHROPIC_API_KEY", "Keys", "Anthropic API key (Clara)",
     "Clara's own conversation model. Not the box's key: that one lives in the box (clara-box-key).", "secret", "host"),
    ("ANTHROPIC_WORKSPACE_ID", "Keys", "Anthropic workspace id",
     "Only if the key above is organisation-wide rather than workspace-scoped.", "text", "host"),
    ("ELEVENLABS_API_KEY", "Keys", "ElevenLabs API key", "For the ElevenLabs voice or speech-to-text.", "secret", "host"),
    ("OPENAI_API_KEY", "Keys", "OpenAI API key", "Only for the OpenAI brain or speech-to-text.", "secret", "host"),
    ("FISH_AUDIO_API_KEY", "Keys", "Fish Audio API key", "Only for Fish Audio voices.", "secret", "host"),
    ("AGENT_RELAY_TOKEN", "Keys", "Relay token",
     "A shared secret between Clara and the relay. Needed only if the relay listens beyond this computer.", "secret", "both"),
    ("AGENT_RELAY_URL", "Clara", "Where her jobs go",
     "The Claude Code relay. http://127.0.0.1:8032 on this computer.", "text", "host"),
    ("SATELLITE_ALLOW", "Clara", "Boards allowed (from .env)",
     "Board ids, comma-separated. Easier on the Boards tab, which also lists refused boards.", "text", "host"),
    ("PIPER_VOICE", "Clara", "Local voice (Piper)", "A Piper voice name, e.g. en_US-hfc_female-medium.", "text", "host"),
    ("OLLAMA_HOST", "Clara", "Ollama address", "Only for a local model through Ollama.", "text", "host"),
    ("OLLAMA_MODEL", "Clara", "Ollama model", "Only for a local model through Ollama.", "text", "host"),
    ("CLARA_BOX_CLAUDE_JOB_USD", "Claude Code", "Most a job may cost ($)",
     "Passed to Claude Code as its budget for one job.", "number", "relay"),
    ("CLARA_BOX_CLAUDE_MONTHLY_USD", "Claude Code", "Monthly allowance ($)",
     "New jobs are refused once the month's total would pass it. The spend limit on the box's key in "
     "the Anthropic Console is the cap that always holds.", "number", "relay"),
]
KNOWN_KEYS = {k[0] for k in KNOWN}


def is_secret(key: str) -> bool:
    for k in KNOWN:
        if k[0] == key:
            return k[4] == "secret"
    return bool(SECRET_RE.search(key))


def _read_lines(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def parse(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in _read_lines(path):
        t = line.strip()
        if not t or t.startswith("#") or "=" not in t:
            continue
        if t.startswith("export "):
            t = t[7:]
        k, _, v = t.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _write(path: str, key: str, value: Optional[str]) -> None:
    """Set key=value in place (or append it); value None removes the line. Atomic, 0600."""
    lines, done = _read_lines(path), False
    out = []
    for line in lines:
        t = line.strip()
        t2 = t[7:] if t.startswith("export ") else t
        if not t.startswith("#") and "=" in t2 and t2.partition("=")[0].strip() == key:
            if value is not None and not done:
                out.append(f"{key}={value}")
            done = True
            continue
        out.append(line)
    if value is not None and not done:
        out.append(f"{key}={value}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _clean(value: str) -> str:
    v = str(value)
    if "\n" in v or "\r" in v or "\x00" in v:
        raise ValueError("a setting is one line")
    return v.strip()


class SettingsStore:
    def __init__(self, env_path: str = ENV_PATH, host_env: str = HOST_ENV, writable: bool = True):
        self.env_path, self.host_env = env_path, host_env
        self.writable = writable            # False when the page is open to the network
        self.pending: set = set()           # what has to restart for saved changes to apply

    # -- reading ---------------------------------------------------------------------------------
    def view(self) -> Dict[str, Any]:
        env = parse(self.env_path)
        rows = []
        for key, group, label, help_, kind, applies in KNOWN:
            rows.append(self._row(key, env, group, label, help_, kind, applies))
        for key in sorted(k for k in env if k not in KNOWN_KEYS):
            rows.append(self._row(key, env, "Other", key, "Set in .env.", "secret" if is_secret(key) else "text", "host"))
        return {"settings": rows, "host_args": parse(self.host_env).get("CLARA_ARGS", ""),
                "host_env": self.host_env.replace(os.path.expanduser("~"), "~"),
                "services": self.services(), "writable": self.writable,
                "pending": sorted(self.pending), "under_systemd": bool(os.environ.get("INVOCATION_ID"))}

    @staticmethod
    def _row(key, env, group, label, help_, kind, applies) -> Dict[str, Any]:
        present = key in env and env[key] != ""
        return {"key": key, "group": group, "label": label, "help": help_, "kind": kind, "applies": applies,
                "set": present, "value": None if kind == "secret" else env.get(key, "")}

    def services(self) -> List[Dict[str, Any]]:
        if not shutil.which("systemctl"):
            return []
        out = []
        for name in SERVICES:
            r = subprocess.run(["systemctl", "--user", "show", name, "-p", "ActiveState", "-p", "UnitFileState",
                                "-p", "LoadState"], capture_output=True, text=True, timeout=5)
            st = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
            if st.get("LoadState") == "not-found":
                continue
            out.append({"name": name, "active": st.get("ActiveState", ""), "enabled": st.get("UnitFileState", "")})
        return out

    # -- writing ---------------------------------------------------------------------------------
    def _check(self) -> None:
        if not self.writable:
            raise PermissionError("settings can only be changed when the control page is on this computer only "
                                  "(not with --web-host 0.0.0.0)")

    def set(self, key: str, value: str) -> Dict[str, Any]:
        self._check()
        if not KEY_RE.fullmatch(key or ""):
            raise ValueError("a setting's name is capital letters, digits and underscores")
        v = _clean(value)
        if key in ("CLARA_BOX_CLAUDE_JOB_USD", "CLARA_BOX_CLAUDE_MONTHLY_USD"):
            if float(v) < 0:
                raise ValueError("a dollar amount cannot be negative")
        if is_secret(key) and not v:
            raise ValueError("to remove a key, use Clear")
        _write(self.env_path, key, v)
        self._mark(key)
        return {"message": f"{key} saved" + ("" if is_secret(key) else f" as {v}")}

    def clear(self, key: str) -> Dict[str, Any]:
        self._check()
        if not KEY_RE.fullmatch(key or ""):
            raise ValueError("not a setting's name")
        _write(self.env_path, key, None)
        self._mark(key)
        return {"message": f"{key} removed"}

    def set_host_args(self, args: str) -> Dict[str, Any]:
        self._check()
        v = _clean(args)
        if not v:
            raise ValueError("her options cannot be empty")
        _write(self.host_env, "CLARA_ARGS", v)
        self.pending.add("clara-host")
        return {"message": "her options saved; restart Clara to use them"}

    def _mark(self, key: str) -> None:
        applies = next((k[5] for k in KNOWN if k[0] == key), "host")
        self.pending |= {"host": {"clara-host"}, "relay": {"clara-relay"}, "both": {"clara-host", "clara-relay"}}[applies]

    def service(self, name: str, on: bool) -> Dict[str, Any]:
        self._check()
        if name != "clara-speaker":
            raise ValueError("only the speaker player can be switched here")
        r = subprocess.run(["systemctl", "--user", "enable" if on else "disable", "--now", name],
                           capture_output=True, text=True, timeout=20)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200])
        return {"message": f"{name} {'on' if on else 'off'}"}

    def restart(self) -> Dict[str, Any]:
        """Restart the relay and Clara (this program). --no-block: systemd does it after we answer."""
        self._check()
        if not os.environ.get("INVOCATION_ID"):
            return {"error": "Clara is not running as a service here; restart her by hand "
                             "(or install the services: services/install.sh)"}
        names = [n for n in ("clara-relay", "clara-host") if n in {s["name"] for s in self.services()}]
        subprocess.Popen(["systemctl", "--user", "--no-block", "restart"] + names,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.pending.clear()
        return {"message": "restarting: the page comes back in a few seconds"}
