#!/usr/bin/env python3
"""
claude_box_relay.py — Clara's errands go straight to Claude Code in her box (the
clara-box VM), with no other agent harness in between.

    python tools/claude_box_relay.py                     # listens on 127.0.0.1:8032
    python tools/claude_box_relay.py --costs [DAYS]      # what the jobs have cost, then exit
    voice_loop.py ... --agent-url http://127.0.0.1:8032

It speaks the talker errand protocol (version 2, see talker/errands.py):
    POST /tasks              {"id", "task", "context", "from"}  -> {"id"}
    GET  /tasks/{id}         -> {"id", "state", "summary", "result", "question", ...}
    POST /tasks/{id}/answer  {"answer", "by"}   resumes a task that asked a question
    GET  /tasks, GET /health
    GET  /box/apps           the servers running in the box (box-app list), for the control page
    POST /box/apps/{stop|keep|unkeep|restart|forget|stop-port|adopt}  {"name", "port", "keep"}

How a task runs:
  * Claude Code runs headless in the box (~/bin/cc-run: tmux window cc-<id>, auto
    mode, its own API key, a per-job budget). Its ~/.claude/CLAUDE.md tells it the
    answers are spoken, to clarify before starting when it must, and to begin a
    final answer with "QUESTION:" when it needs the owner. That becomes needs_input:
    Clara asks, and the answer resumes the same Claude Code session.
  * A task that arrives within RELAY_FOLLOWUP_MIN minutes of the last one continues
    that Claude Code conversation, so "now add tests too" works.
    "new job" / "start fresh" in the task text starts a new one.
  * "sonnet" or "opus" in the task picks that model; otherwise haiku (cheap).
  * "How much has Claude Code cost" is answered from the spending ledger, with no
    Claude Code run. The ledger is on this machine, where the box cannot edit it.
  * Short requests about the pages the box serves ("what's running in your box", "stop the
    scatter plot", "keep the weather page", "bring back the scatter plot", "what's in your
    library") are done with box-app directly, also with no Claude Code run. Anything less clear
    goes to Claude Code, which can use box-app itself.
  * Tasks run one at a time, so two never write to the same session.

Settings (environment):
    RELAY_HOST 127.0.0.1   RELAY_PORT 8032   RELAY_STATE ~/.local/share/clara-box/relay_tasks.json
    RELAY_FOLLOWUP_MIN 20  RELAY_MINUTES 15 (time limit per run)
    CLARA_BOX_CLAUDE_JOB_USD 1.00   CLARA_BOX_CLAUDE_MONTHLY_USD 20
    CLARA_BOX_KEY / CLARA_BOX_KNOWN_HOSTS / CLARA_BOX_PORT (defaults under ~/.local/share/clara-box)
    AGENT_RELAY_TOKEN  optional shared secret; the voice host sends it as X-Relay-Token

Who may call it: only programs on this machine. Requests that come from a web page (an Origin
header, a form content type, or a Host that is not this machine's loopback name) are refused,
so a page open in your browser cannot start a job or read the task list.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOX_DIR = Path.home() / ".local" / "share" / "clara-box"
MODELS = ("haiku", "sonnet", "opus")
INPUT_TTL = 1800.0
MAX_BODY = 256 * 1024              # a request body larger than this is refused
MAX_RESULT = 20000                 # characters of a Claude Code answer that are kept
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
APP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
BOX_PORTS = range(8100, 8120)
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")   # a session id goes on a command line: nothing else passes

COST_RE = re.compile(r"\b(cost|costs|spent|spend|spending|bill)\b", re.I)
# ... and about Claude Code itself: "keep a log of what I spend on coffee" is a job
COST_ABOUT_RE = re.compile(r"\b(claude|you|your|yourself|the box|jobs?|so far|this month|today|allowance|budget)\b", re.I)
BUILD_RE = re.compile(r"\b(build|make|create|write|track(er|ing)?|app|page|script)\b", re.I)
FRESH_RE = re.compile(r"\b(new job|new task|start fresh|fresh start|from scratch|separate job)\b", re.I)
QUESTION_RE = re.compile(r"^\s*\**QUESTION\**\s*:\s*", re.I)
# Clara's planner adds this for confirmations ("send this text?"). For Claude Code it would
# also stop the clarifying questions the owner wants, so it is softened on the way through.
APPROVED_RE = re.compile(r"APPROVED IN ADVANCE:.*?then report what was done\.", re.S)
APPROVED_NOTE = ("(The owner asked for this themselves, so you need no permission to act. Still ask if "
                 "the request is genuinely unclear.)")

PROMPT = """{task}
{context}
(From Clara, the owner's voice assistant. Your final answer is spoken to the owner.)"""


def _env_money(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


class Ledger:
    """Per-job costs, one JSON line each (box/clara-box-costs prints them)."""

    def __init__(self, path: Path):
        self.path = path

    def entries(self) -> list:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    def add(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:
            f.write(json.dumps(entry) + "\n")

    def spent(self, since: float) -> float:
        return sum(float(e.get("cost") or 0) for e in self.entries() if float(e.get("at") or 0) >= since)

    @staticmethod
    def month_start(now: float) -> float:
        return dt.datetime.fromtimestamp(now).replace(day=1, hour=0, minute=0, second=0,
                                                      microsecond=0).timestamp()

    def spoken_summary(self, now: float | None = None) -> str:
        now = now or time.time()
        day0 = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        month = self.spent(self.month_start(now))
        cap = _env_money("CLARA_BOX_CLAUDE_MONTHLY_USD", 20.0)
        jobs = [e for e in self.entries() if float(e.get("at") or 0) >= self.month_start(now)]
        return (f"Claude Code has cost {_dollars(month)} this month over {len(jobs)} jobs, "
                f"{_dollars(self.spent(day0))} of it today, out of a {_dollars(cap)} monthly allowance.")


def _dollars(x: float) -> str:
    if x < 0.01:
        return "less than a cent"
    return f"${x:.2f}"


class BoxRunner:
    """One Claude Code run in the box via ~/bin/cc-run. Returns its result JSON."""

    def ssh(self) -> list:
        return ["ssh", "-i", os.environ.get("CLARA_BOX_KEY", str(BOX_DIR / "keys" / "clara")),
                "-p", os.environ.get("CLARA_BOX_PORT", "2222"),
                "-o", f"UserKnownHostsFile={os.environ.get('CLARA_BOX_KNOWN_HOSTS', str(BOX_DIR / 'known_hosts'))}",
                "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "clara@127.0.0.1"]

    def ensure_up(self) -> None:
        if subprocess.run(self.ssh() + ["true"], capture_output=True, timeout=15).returncode == 0:
            return
        subprocess.run(["virsh", "-q", "-c", "qemu:///session", "start", "clara-box"],
                       capture_output=True, timeout=30)
        deadline = time.time() + 90
        while time.time() < deadline:
            if subprocess.run(self.ssh() + ["true"], capture_output=True, timeout=15).returncode == 0:
                return
            time.sleep(2)
        raise RuntimeError("the box did not start")

    def box_app(self, args: list, timeout: float = 30) -> tuple:
        """box-app in the box, for the control page. Returns (exit code, output). Does not start a
        stopped box: looking at the list should not boot a VM."""
        try:
            p = subprocess.run(self.ssh() + ["box-app"] + args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "the box did not answer"
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        if p.returncode == 255:
            return 255, "the box is off or unreachable"
        return p.returncode, out or err

    def run(self, prompt: str, model: str, budget: float, session: str, minutes: int) -> dict:
        self.ensure_up()
        tid = secrets.token_hex(3)
        if model not in MODELS or (session and not SESSION_RE.fullmatch(session)):
            raise RuntimeError("refused: bad model or session id")   # both reach a shell in the box
        env = [f"CC_MODEL={model}", f"CC_BUDGET={budget:.2f}"] + ([f"CC_RESUME={session}"] if session else [])
        secs = minutes * 60
        p = subprocess.run(self.ssh() + ["env"] + env + ["bin/cc-run", tid, str(secs)],
                           input=prompt.encode(), capture_output=True, timeout=secs + 90)
        lines = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        try:
            r = json.loads(lines[-1])
        except (IndexError, ValueError):
            raise RuntimeError(f"no result from Claude Code (exit {p.returncode})")
        r["_task"] = tid
        return r


class Relay:
    def __init__(self, state_path: Path, ledger: Ledger, runner: BoxRunner,
                 followup_min: float = 20.0, minutes: int = 15):
        self.state_path = state_path
        self.ledger = ledger
        self.runner = runner
        self.followup_s = followup_min * 60
        self.minutes = minutes
        self.tasks: dict = {}
        self.last_session = ""
        self.last_at = 0.0
        self._lock = threading.Lock()
        self._one = threading.Lock()       # one Claude Code run at a time
        self._load()

    # ── persistence ────────────────────────────────────
    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text())
            self.tasks = data.get("tasks", {})
            self.last_session = data.get("last_session", "")
            self.last_at = float(data.get("last_at") or 0)
        except (FileNotFoundError, ValueError):
            pass
        for t in self.tasks.values():
            if t["state"] in ("queued", "running", "answering"):
                t["state"], t["summary"] = "failed", "The relay restarted before this finished."
                t["error_kind"] = "transient"

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:   # task texts: owner only
            f.write(json.dumps({"tasks": self.tasks, "last_session": self.last_session,
                                "last_at": self.last_at}, indent=1))
        os.replace(tmp, self.state_path)

    def _set(self, tid: str, **fields) -> None:
        with self._lock:
            self.tasks[tid].update(fields, updated=time.time())
            self._save()

    def get(self, tid: str) -> dict | None:
        with self._lock:
            t = self.tasks.get(tid)
            if t and t["state"] == "needs_input" and time.time() > float(t.get("expires") or 0):
                t.update(state="failed", error_kind="needs_input_expired", updated=time.time(),
                         summary="Claude Code asked a question that was not answered in time.")
                self._save()
            return dict(t) if t else None

    # ── intake ─────────────────────────────────────────
    def submit(self, task: str, context: str = "", tid: str = "", sender: str = "") -> str:
        tid = tid or secrets.token_hex(2)
        entry = {"id": tid, "task": task, "context": context, "from": sender, "state": "queued",
                 "summary": "", "result": "", "created": time.time(), "updated": time.time()}
        with self._lock:
            self.tasks[tid] = entry
            self._save()
        threading.Thread(target=self._work, args=(tid, None), daemon=True, name=f"task-{tid}").start()
        return tid

    def answer(self, tid: str, answer: str, by: str = "owner") -> str:
        """'' when accepted, else the error text."""
        with self._lock:
            t = self.tasks.get(tid)
            if not t or t["state"] != "needs_input" or time.time() > float(t.get("expires") or 0):
                return "task is not waiting for input"
            t.update(state="answering", answer=answer, answered_by=by, updated=time.time())
            for k in ("question", "options", "expires"):
                t.pop(k, None)
            self._save()
        threading.Thread(target=self._work, args=(tid, answer), daemon=True, name=f"answer-{tid}").start()
        return ""

    # ── the work ───────────────────────────────────────
    def _pick_model(self, text: str) -> str:
        for m in ("opus", "sonnet"):
            if re.search(rf"\b{m}\b", text, re.I):
                return m
        return "haiku"

    def _work(self, tid: str, answer: str | None) -> None:
        t = self.tasks[tid]
        text = t["task"]
        if answer is None:
            try:
                spoken = box_voice(self.runner, text)
            except Exception as e:                       # never let the shortcut lose a job
                print(f"[relay] {tid} box shortcut failed ({e}); passing it to Claude Code")
                spoken = None
            if spoken:
                self._set(tid, state="done", summary=spoken, result=spoken)
                print(f"[relay] {tid} done with box-app, no Claude Code run: {spoken[:90]}")
                return
        if answer is None and COST_RE.search(text) and COST_ABOUT_RE.search(text) and not BUILD_RE.search(text) \
                and not re.search(r"\b(log|record|keep)\b", text, re.I):
            s = self.ledger.spoken_summary()
            self._set(tid, state="done", summary=s, result=s)
            print(f"[relay] {tid} cost question answered from the ledger")
            return
        with self._one:
            self._set(tid, state="running")
            now = time.time()
            month = self.ledger.spent(Ledger.month_start(now))
            cap = _env_money("CLARA_BOX_CLAUDE_MONTHLY_USD", 20.0)
            budget = _env_money("CLARA_BOX_CLAUDE_JOB_USD", 1.0)
            if month + budget > cap:
                self._set(tid, state="failed", error_kind="refused",
                          summary=f"Claude Code's monthly allowance is used up: {_dollars(month)} of "
                                  f"{_dollars(cap)}. The owner can raise it.")
                return
            if answer is not None:
                session = t.get("session") or ""
                prompt = f"The owner answered: {answer}"
            else:
                fresh = bool(FRESH_RE.search(text))
                session = "" if fresh or now - self.last_at > self.followup_s else self.last_session
                ctx = f"\nWhat the owner had just said: {t['context']}" if t.get("context") else ""
                prompt = PROMPT.format(task=APPROVED_RE.sub(APPROVED_NOTE, text), context=ctx)
            model = t.get("model") or self._pick_model(text)
            print(f"[relay] {tid} running ({model}{', continuing ' + session[:8] if session else ', new session'}): {text[:90]}")
            try:
                r = self.runner.run(prompt, model, budget, session, self.minutes)
            except Exception as e:
                self._set(tid, state="failed", error_kind="transient",
                          summary=f"I couldn't reach Claude Code in the box: {e}.")
                print(f"[relay] {tid} failed: {e}")
                return
            cost = r.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                self.ledger.add({"at": round(now, 1), "task": r.get("_task"), "model": model,
                                 "cost": round(float(cost), 4), "turns": r.get("num_turns"),
                                 "seconds": round(time.time() - now), "session": r.get("session_id"),
                                 "error": bool(r.get("is_error")), "what": text.splitlines()[0][:80],
                                 "via": "relay"})
            sid = str(r.get("session_id") or "")
            sid = sid if SESSION_RE.fullmatch(sid) else session
            if sid:
                self.last_session, self.last_at = sid, time.time()
            result = _untrusted(str(r.get("result") or ""))
            fields = {"session": sid, "model": model, "cc_task": r.get("_task")}
            if r.get("is_error"):
                self._set(tid, state="failed", error_kind="tool_error", result=result,
                          summary=f"Claude Code hit a problem: {result[:300] or r.get('subtype') or 'unknown error'}",
                          **fields)
                print(f"[relay] {tid} failed: {result[:120]}")
            elif QUESTION_RE.match(result):
                q = QUESTION_RE.sub("", result, count=1).strip()
                self._set(tid, state="needs_input", question=q, options=[], result=result,
                          summary="Claude Code has a question before it goes on.",
                          expires=time.time() + INPUT_TTL, **fields)
                print(f"[relay] {tid} needs input: {q[:120]}")
            else:
                self._set(tid, state="done", result=result, summary=_spoken(result), **fields)
                print(f"[relay] {tid} done: {result[:120]}")


def box_apps(runner) -> tuple:
    """(http code, body) for GET /box/apps."""
    code, out = runner.box_app(["list", "--json"])
    if code != 0:
        return 502, {"error": out or f"box-app failed ({code})"}
    try:
        return 200, json.loads(out)
    except ValueError:
        return 502, {"error": "box-app gave no list"}


def box_action(runner, action: str, body: dict) -> tuple:
    """(http code, body) for POST /box/apps/<action>. Only fixed actions, a checked name and a port
    in the owner's range reach the box's command line."""
    name = str(body.get("name") or "").strip().lower()
    port = body.get("port")
    port = int(port) if str(port or "").isdigit() else 0
    if action in ("stop", "keep", "unkeep", "restart", "forget"):
        if not APP_NAME_RE.fullmatch(name):
            return 400, {"error": "a server name is lowercase letters, digits and dashes"}
        args = [action, name] + (["--keep"] if action == "restart" and body.get("keep") else [])
    elif action == "stop-port":
        if port not in BOX_PORTS:
            return 400, {"error": "the port must be in 8100-8119"}
        args = ["stop-port", str(port)]
    elif action == "adopt":
        if not APP_NAME_RE.fullmatch(name) or port not in BOX_PORTS:
            return 400, {"error": "adopt needs a name (lowercase letters, digits, dashes) and a port in 8100-8119"}
        args = ["adopt", name, str(port)] + (["--keep"] if body.get("keep") else [])
    else:
        return 404, {"error": "not found"}
    code, out = runner.box_app(args)
    return (200, {"message": out}) if code == 0 else (400 if code == 1 else 502, {"error": out})


# ── the box's pages, by voice ─────────────────────────────────────────────────
VOICE_PREFIX_RE = re.compile(r"^(?:\s*(?:please|ask claude code to|have claude code|tell claude code to|"
                             r"can you|could you|in (?:the|your) box,?|on (?:the|your) box,?)\s+)+", re.I)
VOICE_BUILD_RE = re.compile(r"\b(build|create|write|add|change|fix|update|edit|redesign|refactor|improve|"
                            r"research|find|search|look up|compare|log|track|"
                            r"make (?:a|an|me|it look|some|the page)\b|so that|which shows|that shows)\b", re.I)
VOICE_LIST_RE = re.compile(r"\b(what|which)\b.{0,40}\b(running|up|serving|served|open|live)\b|"
                           r"\b(list|show)\b.{0,40}\b(running|served|servers|pages|apps|sites)\b", re.I)
VOICE_LIBRARY_RE = re.compile(r"\blibrary\b|\bwhat (?:pages|apps|sites|things) (?:have|has|did) (?:you|it|claude code)"
                              r" (?:built|made)\b|\bwhat have you built\b", re.I)
VOICE_ACTIONS = [
    ("unkeep", re.compile(r"^(?:make|set)\b.{0,50}\btemporary\b|^(?:let)\b.{0,50}\b(?:expire|stop on its own)\b", re.I)),
    ("stop", re.compile(r"^(?:stop|shut (?:down|off)|turn off|kill|close)\b", re.I)),
    ("keep", re.compile(r"^keep\b", re.I)),
    ("restart", re.compile(r"^(?:restart|bring back|reopen|re-open|turn\b.{0,40}\bback on|"
                           r"(?:start|open|run)\b.{0,50}\bagain)\b", re.I)),
    ("open", re.compile(r"^(?:open|pull up)\b", re.I)),
]
# a listing has to be about the box itself, not servers in general
VOICE_SERVER_WORDS = re.compile(r"\b(box|you(?:'re| are)? (?:serving|running|hosting)|have (?:you )?(?:got )?(?:running|up)|"
                                r"localhost|ports? 81[01]\d)\b", re.I)
_STOP_WORDS = set("""the a an my your our this that it its one ones please in on of to for with and or which is are
    page pages app apps server servers site sites box running up again back now thing claude code also too
    there here just me us all can you could would will be
    stop shut down off turn kill close keep make set temporary let expire restart bring reopen re start
    open pull run again what whats which s list library have has built made""".split())


def _words(text: str) -> set:
    t = re.sub(r"\b(three|3)[ -]?d\b", "3d", text.lower())
    out = set()
    for w in re.findall(r"[a-z0-9]+", t):
        out.add(w)
        for part in re.findall(r"[a-z]+|[0-9]+[a-z]?", w):   # scatter3d -> scatter, 3d
            out.add(part)
        if len(w) > 3 and w.endswith("s"):
            out.add(w[:-1])
    return out - _STOP_WORDS


def _pick(entries: list, text: str) -> tuple:
    """(entry, None) for the one entry the words point at, or (None, why) when none or several do."""
    want = _words(text)
    if not want and entries:                             # "stop the page": fine if there is only one
        return (entries[0], None) if len(entries) == 1 else (None, "several")
    scored = []
    for e in entries:
        have = _words(e["name"].replace("-", " ")) | _words(e.get("about") or "") | ({str(e["port"])} if e.get("port") else set())
        scored.append((len(want & have), e))
    best = max((s for s, _ in scored), default=0)
    hits = [e for s, e in scored if s == best and s > 0]
    if len(hits) == 1:
        return hits[0], None
    if not entries:
        return None, "none"
    return None, "several" if hits else "none"


def _spoken_name(e: dict) -> str:
    """'the 3D scatter plot', from its one-line description (or its name)."""
    about = (e.get("about") or "").strip().rstrip(".")
    if not about:
        return "the " + e["name"].replace("-", " ")
    return re.sub(r"^(a|an)\s+", "the ", about, flags=re.I)


def _spoken_list(names: list) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def box_voice(runner, text: str):
    """A spoken answer for a short request about the box's pages, done with box-app; None to leave it
    to Claude Code."""
    t = VOICE_PREFIX_RE.sub("", text.strip()).strip().rstrip(".!?")
    if not t or VOICE_BUILD_RE.search(t) or not hasattr(runner, "box_app"):
        return None
    # Clara's planner rewords a request at length. Looking is read-only, so a long request may
    # still be a listing; changing something must be short and name one page.
    words = len(t.split())
    action = next((a for a, rx in VOICE_ACTIONS if rx.search(t)), None) if words <= 30 else None
    listing = action is None and VOICE_LIST_RE.search(t) and VOICE_SERVER_WORDS.search(t)
    library = action is None and not listing and VOICE_LIBRARY_RE.search(t) and words <= 30
    if not (action or listing or library):
        return None
    code, out = runner.box_app(["list", "--json"])
    if code != 0:
        return "I can't reach my box right now; it may be off." if code == 255 else None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    apps, lib = data.get("apps", []), data.get("library", [])
    about = {e["name"]: e.get("about") for e in lib if e.get("about")}
    apps = [dict(a, about=a.get("about") or about.get(a["name"], "")) for a in apps]   # descriptions live in the library
    if listing:
        if not apps:
            return "Nothing is running in my box right now."
        parts = [f"{_spoken_name(a)} at localhost port {a['port']}, "
                 + ("kept" if a.get("keep") else f"temporary, {max(0, round(a.get('expires_in_h') or 0))} hours left")
                 for a in apps]
        n = len(apps)
        return f"{'One page is' if n == 1 else f'{n} pages are'} running in my box: " + "; ".join(parts) + "."
    if library:
        if not lib:
            return "My library is empty: nothing has been served from the box yet."
        stopped = [e for e in lib if not e.get("running")]
        names = [_spoken_name(e) for e in lib[:8]]
        more = f", and {len(lib) - 8} more" if len(lib) > 8 else ""
        return (f"My library has {len(lib)}: {_spoken_list(names)}{more}. "
                + (f"{len(lib) - len(stopped)} running, {len(stopped)} stopped; I can bring any of them back."
                   if stopped else "All of them are running."))
    running = {a["name"]: a for a in apps}
    if action in ("stop", "keep", "unkeep"):
        e, why = _pick(apps, t)
        if e is None:
            gone, _ = _pick([x for x in lib if not x.get("running")], t)
            if gone and why == "none":
                return f"{_spoken_name(gone).capitalize()} isn't running. It's in my library if you want it back."
            if why == "several":
                return f"Which one? Running now: {_spoken_list([_spoken_name(a) for a in apps])}."
            return None                                     # not about a page we have: a job for Claude Code
        code, out = runner.box_app([action, e["name"]])
        if code != 0:
            return f"That didn't work: {out}"
        return {"stop": f"Stopped {_spoken_name(e)}. Its files are kept, and I can bring it back any time.",
                "keep": f"{_spoken_name(e).capitalize()} is kept: it stays up, even after the box restarts.",
                "unkeep": f"{_spoken_name(e).capitalize()} is temporary again: it stops by itself in a day."}[action]
    # restart / open: anything in the library
    e, why = _pick(lib, t)
    if e is None:
        if not lib:
            return "My library is empty, so there is nothing to bring back." if action == "restart" else None
        if action == "open" and why == "none":
            return None                                     # "open a page that ..." is a job, not a lookup
        return (f"Which one? {_spoken_list([_spoken_name(x) for x in lib[:6]])}?" if why == "several"
                else "I don't have a page like that in my library.")
    if e["name"] in running:
        return f"{_spoken_name(e).capitalize()} is already running, at localhost port {running[e['name']]['port']}."
    if not e.get("dir_exists", True):
        return f"I can't bring back {_spoken_name(e)}: its folder is gone."
    code, out = runner.box_app(["restart", e["name"]])
    if code != 0:
        return f"That didn't work: {out}"
    m = re.search(r"localhost:(\d+)", out)
    where = f" at localhost port {m.group(1)}" if m else ""
    return f"{_spoken_name(e).capitalize()} is back{where}. It's temporary: it stops in a day unless you say keep it."


def _untrusted(text: str) -> str:
    """Claude Code's answer may repeat text from a web page. Clara's own tool syntax ({{task …}},
    {{tool …}}) is taken out so such text cannot be passed to her as a command, and the length
    is capped."""
    return re.sub(r"\{\{|\}\}", "", text).strip()[:MAX_RESULT]


def _spoken(text: str, limit: int = 700) -> str:
    """The answer is already written for the ear (CLAUDE.md); keep it to a few
    sentences and strip any markdown that slipped in."""
    t = re.sub(r"`{3}.*?`{3}", " ", text, flags=re.S)
    t = re.sub(r"[*_#`>|]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[:end + 1] if end > limit // 3 else cut.rstrip() + "…") + " The full answer is in the result."


def make_handler(relay: Relay, token: str = "", extra_host: str = ""):
    allowed_hosts = set(LOCAL_HOSTS) | ({extra_host} if extra_host else set())

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _refused(self, post: bool = False) -> bool:
            """True (and an error sent) unless this is a local program's request."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") \
                else (self.headers.get("Host") or "").split("]")[0] + "]"
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if host not in allowed_hosts or self.headers.get("Origin") or \
                    self.headers.get("Sec-Fetch-Site") not in (None, "none") or (post and ctype != "application/json"):
                self._json(403, {"error": "local programs only"})
                return True
            if token and not hmac.compare_digest(self.headers.get("X-Relay-Token") or "", token):
                self._json(401, {"error": "bad or missing X-Relay-Token"})
                return True
            return False

        def _body(self) -> dict | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > MAX_BODY:
                return None
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
                return b if isinstance(b, dict) else None
            except ValueError:
                return None

        def do_GET(self) -> None:
            if self._refused():
                return
            path = self.path.split("?")[0].rstrip("/")
            if path == "/health":
                st = [t["state"] for t in relay.tasks.values()]
                return self._json(200, {"ok": True, "running": st.count("running") + st.count("answering"),
                                        "needs_input": st.count("needs_input"), "protocol": 2,
                                        "features": ["needs_input", "answer", "error_kind"]})
            if path == "/tasks":
                return self._json(200, sorted(relay.tasks.values(), key=lambda t: t["created"]))
            if path == "/box/apps":
                return self._json(*box_apps(relay.runner))
            if path.startswith("/tasks/"):
                tid = path[len("/tasks/"):]
                t = relay.get(tid)
                if t:
                    return self._json(200, t)
                # A task this relay never saw (sent to another backend before a switch): say it
                # is gone rather than 404, which the poller treats as "backend unreachable".
                return self._json(200, {"id": tid, "state": "failed", "error_kind": "refused",
                                        "summary": "That job went to the old agent before the switch to "
                                                   "Claude Code; ask again if it is still wanted.",
                                        "result": ""})
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self._refused(post=True):
                return
            path = self.path.split("?")[0].rstrip("/")
            body = self._body()
            if body is None:
                return self._json(400, {"error": "bad json"})
            m = re.fullmatch(r"/box/apps/([a-z-]+)", path)
            if m:
                return self._json(*box_action(relay.runner, m.group(1), body))
            m = re.fullmatch(r"/tasks/([^/]+)/answer", path)
            if m:
                ans = str(body.get("answer") or "").strip()
                if not ans:
                    return self._json(400, {"error": "answer is required"})
                err = relay.answer(m.group(1), ans, str(body.get("by") or "owner"))
                return self._json(409, {"error": err}) if err else self._json(200, {"id": m.group(1)})
            if path != "/tasks":
                return self._json(404, {"error": "not found"})
            task = str(body.get("task") or "").strip()
            if not task:
                return self._json(400, {"error": "task is required"})
            tid = relay.submit(task, str(body.get("context") or ""), str(body.get("id") or ""),
                               str(body.get("from") or ""))
            self._json(200, {"id": tid})

        def log_message(self, fmt, *args) -> None:
            pass
    return Handler


def main() -> int:
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default=env("RELAY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("RELAY_PORT", "8032")))
    ap.add_argument("--state", default=env("RELAY_STATE", str(BOX_DIR / "relay_tasks.json")))
    ap.add_argument("--followup-min", type=float, default=float(env("RELAY_FOLLOWUP_MIN", "20")))
    ap.add_argument("--minutes", type=int, default=int(env("RELAY_MINUTES", "15")))
    ap.add_argument("--costs", nargs="?", type=int, const=0, default=None, metavar="DAYS",
                    help="print what Claude Code jobs have cost (and each job of the last DAYS days), then exit")
    a = ap.parse_args()
    if a.costs is not None:
        ledger = Ledger(Path(env("CLARA_BOX_CLAUDE_LEDGER", str(BOX_DIR / "claude-costs.jsonl"))))
        print(ledger.spoken_summary())
        for e in ledger.entries():
            if a.costs and float(e.get("at") or 0) >= time.time() - a.costs * 86400:
                when = dt.datetime.fromtimestamp(float(e.get("at") or 0)).strftime("%m-%d %H:%M")
                print(f"  {when}  ${float(e.get('cost') or 0):.3f}  {e.get('model', '')}")
        return 0
    relay = Relay(Path(a.state), Ledger(Path(env("CLARA_BOX_CLAUDE_LEDGER", str(BOX_DIR / "claude-costs.jsonl")))),
                  BoxRunner(), a.followup_min, a.minutes)
    os.umask(0o077)
    try:
        BOX_DIR.chmod(0o700)                      # the VM disk, its key and the task texts live here
    except OSError:
        pass
    token = (env("AGENT_RELAY_TOKEN") or "").strip()
    if a.host not in ("127.0.0.1", "localhost", "::1") and not token:
        print("[relay] refusing to listen beyond this machine without AGENT_RELAY_TOKEN set")
        return 2
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(relay, token, a.host))
    srv.daemon_threads = True
    print(f"[relay] Claude Code in clara-box, on http://{a.host}:{a.port}  ({len(relay.tasks)} tasks on file)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
