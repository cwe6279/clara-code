# clara-code: a voice assistant for Claude Code

Talk to a small ESP32 board; Claude Code does the work. Clara is the voice in between. You ask
out loud for research, code, a web page or a file. She hands the job to Claude Code, which runs
in its own locked-down virtual machine, and tells you the result when it is done. If Claude
Code needs a decision, she asks you and passes your answer back. A follow-up ("now add tests")
continues the same job.

```
you ──voice──> ESP32 board ──Wi-Fi──> voice host (Clara) ──job──> relay ──ssh──> VM: Claude Code
you <──voice── ESP32 board <──────── "it's done: …" / "it asks: …" <──────────── final answer
```

## The pieces

| Piece | What it does | Where |
|---|---|---|
| Board | Microphone, speaker, wake word; optionally a screen and camera | firmware in [cwe6279/clara-esp32](https://github.com/cwe6279/clara-esp32), guide in [docs/SATELLITE_ESP32.md](docs/SATELLITE_ESP32.md) |
| Voice host | Speech to text, Clara's conversation model, text to speech | `voice_loop.py`, `talker/`, her character in `faces/clara/` |
| Relay | Turns Clara's jobs into Claude Code runs; follow-ups, questions, spending caps | `tools/claude_box_relay.py` |
| Box | The VM Claude Code works in: no sudo, internet but no access to your network | `box/`, guide in [docs/CLARA_BOX.md](docs/CLARA_BOX.md) |

Two models are involved. Clara herself is a fast conversational model (any Claude model, or a
local one) that keeps talk quick. Claude Code is called only for jobs, on Haiku unless you ask
for Sonnet or Opus, and uses its own API key with its own spending limit.

**Claude Code is required.** The default install runs Claude Code in the box with an Anthropic
API key, and the relay is written for its command line. Claude Code itself can be pointed at
another endpoint, including a local model server that speaks its API, through its own settings
(`ANTHROPIC_BASE_URL` and the model names, in the `env` block of `~/.claude/settings.json` in
the box). That is Claude Code's feature, not something this project adds, and it has not been
tested here. The box's firewall blocks your local network, so a model server on your LAN
would also need a rule for it.

## Setup

1. **Voice host.**
   ```bash
   sudo dnf install ffmpeg portaudio-devel        # Debian/Ubuntu: sudo apt install ffmpeg portaudio19-dev
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env                           # then put your Anthropic API key in it
   ```
2. **Box.** Build the VM and install Claude Code in it: [docs/CLARA_BOX.md](docs/CLARA_BOX.md).
   Then run `clara-box-key` to give it its own API key.
3. **Board.** Flash xiaozhi-esp32 for your board and point it at the host:
   [docs/SATELLITE_ESP32.md](docs/SATELLITE_ESP32.md).
4. **Run.**
   ```bash
   python tools/claude_box_relay.py &
   python voice_loop.py --face clara --satellite --no-window --agent-url http://127.0.0.1:8032
   ```

## Using it

- **Give a job:** "Clara, have Claude Code build a page that shows the weather for the week."
  She says she is on it, and announces the result when it lands. The board stays connected
  while a job is out.
- **Follow up:** anything within 20 minutes continues the same Claude Code conversation. Say
  "new job" to start fresh.
- **Pick a model:** say "use Sonnet" or "use Opus" in the request.
- **Check spending:** "What has Claude Code cost so far?", or `clara-box-costs` on the host.
- **Watch it work:** `clara-box` opens the VM's tmux session; each job is a window.
- **See a page it built:** servers in the box appear on the host at `http://localhost:8100` to
  `8119`.

Limits are set in the environment: `CLARA_BOX_CLAUDE_JOB_USD` (default 1.00 per job) and
`CLARA_BOX_CLAUDE_MONTHLY_USD` (default 20). The spend limit on the box's API key in the
Anthropic Console is the real cap.

## Hardware

**Your hardware will differ.** The boards, microphones, speakers and computers named in these
docs are the ones this was built and measured on. They are a reference, not a requirement, and
the settings shown for them will not fit most setups. What it needs:

- **A board** that runs [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) and has a
  microphone and a speaker. That firmware supports many ESP32 boards (its `main/boards/`
  folder lists them). Only Clara's on-screen face is tied to one board.
- **A Linux host** that stays on, with CPU virtualization turned on for the box. The VM's size
  (4 CPUs, 4 GB) is two lines in `box/clara-box.xml`.

Developed on Fedora. On Windows, run the host side inside WSL2. That has not been tested here;
expect to need systemd turned on in WSL (`/etc/wsl.conf`), nested virtualization so KVM works
inside it (Windows 11), and `usbipd` if a board has to be attached over USB. The board talks
to the host over Wi-Fi, so it does not care what the host runs.

## Tests

```bash
pytest tests        # no board, sound device, display or VM needed
```

## Where the voice engine came from

The voice host is the Talker engine, forked from
[cwe6279/animation](https://github.com/cwe6279/animation), where it drives animated faces for
projection. That use is not this project's, but the engine's options (speech-to-text and voice
backends, wake words, barge-in, latency tuning) are documented in
[docs/TALKER.md](docs/TALKER.md). Code for the other faces and the projector window is still
in the tree.
