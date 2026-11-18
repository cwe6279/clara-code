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

## Requirements

**Accounts**

- **An Anthropic API key for Clara**, for her conversation model. It goes in `.env`.
- **A second Anthropic API key for the box**, in its own Console workspace with a monthly spend
  limit. Claude Code in the box uses only this one (see Setup, step 3).
- Optional: an ElevenLabs key for a cloud voice. Without one, use the local Piper voice
  (`--tts piper`), as in the run command below.

**Host:** a Linux machine that stays on (developed on Fedora; see [Hardware](#hardware) for WSL2).

- Python 3.10 or newer (developed on 3.13).
- For the voice host: `ffmpeg`, the PortAudio headers and `libopus`.
- For the box: KVM (CPU virtualization turned on in the BIOS), libvirt with the QEMU driver,
  `passt` (the VM's user-mode network), `qemu-img` and `genisoimage`. About 4 GB of free disk
  for the VM, which grows as Claude Code works.
- Optional: a USB microphone and speaker if you want to try it without a board
  (`voice_loop.py` without `--satellite`).

**Board:** an ESP32 board that [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) supports,
with a microphone and a speaker, on the same Wi-Fi as the host. Building the firmware needs
ESP-IDF 6 (about 3 GB with its toolchain), on any machine.

Package names, as used on Fedora (the Debian and Ubuntu names are given but untested):

```bash
# Fedora
sudo dnf install git python3 ffmpeg portaudio-devel opus \
    qemu-kvm libvirt-daemon-driver-qemu libvirt-client passt qemu-img genisoimage
# Debian / Ubuntu
sudo apt install git python3-venv ffmpeg portaudio19-dev libopus0 \
    qemu-system-x86 qemu-utils libvirt-daemon libvirt-clients passt genisoimage
sudo usermod -aG kvm,libvirt "$USER"   # Debian/Ubuntu only; log out and back in
```

Check that KVM is usable: `ls -l /dev/kvm` should exist and be readable by you, and
`virsh -c qemu:///session list` should run without an error.

## Setup

1. **Get the code.**
   ```bash
   git clone https://github.com/cwe6279/clara-code.git && cd clara-code
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env && chmod 600 .env        # then put Clara's Anthropic API key in it
   ```
   `pip install` pulls in the speech models' libraries; the models themselves download on first
   use to `~/.cache` (a few hundred MB for Whisper `small.en` and a Piper voice).
   `pytest tests` checks the install; it needs no board, sound device or VM.
2. **Box.** Build the VM and install Claude Code in it, following
   [docs/CLARA_BOX.md](docs/CLARA_BOX.md) (about 20 minutes, most of it downloads). That also
   installs the `clara-box`, `clara-box-key` and `clara-box-costs` commands.
3. **The box's API key, with a spend limit.** In the Anthropic Console, create a separate
   workspace, give it a monthly spend limit, and create the box's key inside it. Then run
   `clara-box-key` and paste the key when asked (it is not shown or saved on the host).
   Do this before the first job. Claude Code works unattended and reads the web, and the caps in
   this project are counted inside the box, where a misled job could get around them. The
   Console limit is enforced by Anthropic and cannot be bypassed from the box.
4. **Board.** Build xiaozhi-esp32 for your board with its OTA address pointing at the host
   (`http://<host address>:8030/xiaozhi/ota/`), flash it, and join it to your Wi-Fi:
   [docs/SATELLITE_ESP32.md](docs/SATELLITE_ESP32.md). Give the host a fixed address (a DHCP
   reservation on your router), since the board stores it.
5. **Run.**
   ```bash
   python tools/claude_box_relay.py &
   python voice_loop.py --face clara --satellite --no-window --stt whisper --tts piper \
       --agent-url http://127.0.0.1:8032
   ```
   Wake the board and talk. The first start downloads the speech models, so give it a minute.
   For an ElevenLabs voice, drop `--tts piper` and set `ELEVENLABS_API_KEY` in `.env`. More
   options (voices, wake words, models): [docs/TALKER.md](docs/TALKER.md).
6. **Lock it to your board.** Open the control page (http://localhost:8020), go to **Boards**,
   and press **Allow only this board**. From then on every other device on your network is
   refused. A new board you add later shows up under **Refused**; press **Allow** next to it.
   Boards allowed on the page are saved in `settings.json`. You can also list them in `.env` as
   `SATELLITE_ALLOW=<id>,<id>` (or `--satellite-allow`); those can only be removed there.

7. **Keep her running.** Install the user services, so she starts with the computer and comes back
   if anything stops:
   ```bash
   services/install.sh            # add --speaker for a board that plays its speech over USB
   systemctl --user start clara-relay clara-host
   ```
   Her options live in `~/.config/clara-code/host.env` (`CLARA_ARGS=...`, the flags from step 5);
   `systemctl --user restart clara-host` after a change. `journalctl --user -u clara-relay -f`
   follows the relay. Her conversations go to `logs/` (the control page's **Box & files** tab),
   not to the system journal. `clara-speaker` plays the speech of a board with no speaker of its
   own through this computer's sound; once the board has an amplifier,
   `systemctl --user disable --now clara-speaker`.

## Using it

- **Give a job:** "Clara, have Claude Code build a page that shows the weather for the week."
  She says she is on it, and announces the result when it lands. The board stays connected
  while a job is out.
- **Follow up:** anything within 20 minutes continues the same Claude Code conversation. Say
  "new job" to start fresh.
- **Pick a model:** say "use Sonnet" or "use Opus" in the request.
- **Check spending:** "What has Claude Code cost so far?", or `clara-box-costs` on the host.
- **Manage its pages by voice:** "What's running in your box?", "Stop the scatter plot", "Keep
  the weather page", "Bring back the expense tracker", "What's in your library?". These are done
  straight away without a Claude Code run, so they cost nothing. Anything less clear-cut goes to
  Claude Code as a normal job.
- **Watch it work:** `clara-box` opens the VM's tmux session; each job is a window.
- **Look at files without a terminal:** the control page's **Files** tab browses her photos and
  scans, her session logs, and everything in the box's workspace. Preview, download, rename,
  delete (to a trash you can recover from), upload into the box, and see how full its disk is.
- **See a page it built:** servers in the box appear on the host at `http://localhost:8100` to
  `8119`. They stop after a day unless you say "keep it". The control page's **Box** tab lists
  them, and keeps a library of earlier ones you can start again.

Limits are set in the environment: `CLARA_BOX_CLAUDE_JOB_USD` (default 1.00 per job) and
`CLARA_BOX_CLAUDE_MONTHLY_USD` (default 20). They are a convenience. The spend limit you set on
the box's API key in the Anthropic Console (Setup, step 3) is the cap to rely on.

## Hardware

**Your hardware will differ.** The boards, microphones, speakers and computers named in these
docs are the ones this was built and measured on. They are a reference, not a requirement, and
the settings shown for them will not fit most setups. What it needs:

- **A board** that runs [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) and has a
  microphone and a speaker. That firmware supports many ESP32 boards (its `main/boards/`
  folder lists them). Only Clara's on-screen face is tied to one board.
- **A Linux host** that stays on, with CPU virtualization turned on for the box. The VM's size
  (4 CPUs, 4 GB) is two lines in `box/clara-box.xml`.
- **Optionally a small voice host** by the board, such as a Raspberry Pi Zero 2 W, with Claude
  Code's jobs going to the box on the bigger computer: [docs/RPI_ZERO_2W.md](docs/RPI_ZERO_2W.md).

Developed on Fedora. On Windows, run the host side inside WSL2. That has not been tested here;
expect to need systemd turned on in WSL (`/etc/wsl.conf`), nested virtualization so KVM works
inside it (Windows 11), and `usbipd` if a board has to be attached over USB. The board talks
to the host over Wi-Fi, so it does not care what the host runs.

## Privacy

Photos, scans, recordings and session logs stay on your machine: `captures/`, `logs/` and the
rest are in `.gitignore`, and a pre-commit hook (`tools/no_captures.py`) refuses any commit that
adds a capture, or an image or sound file outside `faces/` and `docs/`, even with `git add -f`.
Turn the hook on once per clone:

```bash
git config core.hooksPath .githooks
```

## Tests

```bash
pytest tests        # no board, sound device, display or VM needed
```

## The voice engine

The voice host is the Talker engine, which began as an animated talking face for projection.
Everything it needs is in this repo; it is not tied to the project it came from. Its options
(speech-to-text and voice backends, wake words, barge-in, latency tuning) are documented in
[docs/TALKER.md](docs/TALKER.md). Code for the other faces and the projector window is still in
the tree.
