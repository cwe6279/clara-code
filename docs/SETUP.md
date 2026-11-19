# Setting up Clara from scratch

This page takes you from nothing to talking to Clara: the computer, the box Claude Code works
in, the board, Wi-Fi, and keeping her running. Each step ends with a check, so you know it
worked before you move on. Allow an afternoon. Most of the time goes on downloads and the
first firmware build.

The other docs go deeper on each part. This page links to them where you need more.

## What you need

- **A Linux computer that stays on**, with CPU virtualization turned on in the BIOS. This is
  "the host". It was developed on Fedora. On Windows, use WSL2, which hasn't been tested.
- **A board** that [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) supports, with a
  microphone and a speaker. The reference board is the Waveshare ESP32-S3-Touch-AMOLED-1.8
  V2; Clara's on-screen face runs only on that one. Other boards work as voice only.
- **A USB-C to USB-C data cable.** On the reference board, USB-A to C cables powered it but it
  never showed up as a device.
- **A 2.4 GHz Wi-Fi network** that the board and the host can both reach. ESP32 boards do not
  join 5 GHz networks.
- **Two Anthropic API keys**, from https://console.anthropic.com. One is for Clara's
  conversation. The other is for Claude Code in the box, in its own workspace with a monthly
  spend limit (step 4 explains why). The API is prepaid, so add credit under Plans & Billing
  first.
- Optional: an ElevenLabs key for a cloud voice and cloud speech recognition. Without one,
  Clara uses the local Piper voice and Whisper, which run on the host.

## 1. Prepare the host

Install the system packages:

```bash
# Fedora
sudo dnf install git python3 ffmpeg portaudio-devel opus \
    qemu-kvm libvirt-daemon-driver-qemu libvirt-client passt qemu-img genisoimage
# Debian / Ubuntu (untested)
sudo apt install git python3-venv ffmpeg portaudio19-dev libopus0 \
    qemu-system-x86 qemu-utils libvirt-daemon libvirt-clients passt genisoimage
sudo usermod -aG kvm,libvirt "$USER"   # Debian/Ubuntu only; log out and back in
```

**Give the host a fixed address.** In your router's settings, add a DHCP reservation for the
host, so its IP address never changes. The board saves this address in its firmware. If the
address changes later, the board can't find Clara until you rebuild it. Note the address. It
looks like `192.168.1.50`, and this page calls it `<host address>`.

**Check:** `ls -l /dev/kvm` shows the device and you can read it, and
`virsh -c qemu:///session list` runs without an error.

## 2. Get the code

```bash
git clone https://github.com/cwe6279/clara-code.git && cd clara-code
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```

Open `.env` and set `ANTHROPIC_API_KEY` to Clara's key, the first of the two. If you have an
ElevenLabs key, set `ELEVENLABS_API_KEY` too. Leave the other lines as they are for now.

**Check:** `pytest tests` passes. It needs no board, sound device or VM.

## 3. Build the box

The box is a virtual machine where Claude Code does Clara's jobs. It has no sudo, and it can
reach the internet but not your network. Follow [CLARA_BOX.md](CLARA_BOX.md), "Building it".
It takes about 20 minutes, mostly downloads, and needs about 4 GB of free disk. It also installs
the `clara-box`, `clara-box-key` and `clara-box-costs` commands.

**Check:** `clara-box` opens the box's tmux session. Detach with `Ctrl-b d`.

## 4. Give the box its own key, with a spend limit

In the Anthropic Console, create a **separate workspace** and give it a **monthly spend
limit**. Create the box's key inside that workspace. Then run `clara-box-key` and paste the key
when asked. It isn't shown on screen or saved on the host.

Do this before the first job. Claude Code works unattended and reads the web, so a page could
mislead it. The caps in this project are counted inside the box, where a misled job could get
around them. The Console's limit is enforced by Anthropic and can't be bypassed from the box.

**Check:** `clara-box-costs` prints this month's spending: none yet.

## 5. Build the board's firmware

You need ESP-IDF 6.1, Espressif's build tools. It takes about 3 GB with its compiler.

```bash
mkdir -p ~/esp && cd ~/esp
git clone --depth 1 -b release/v6.1 --recursive --shallow-submodules https://github.com/espressif/esp-idf.git
./esp-idf/install.sh esp32s3
```

**The reference board** uses Clara's firmware fork. It adds her face, her wake word, and the
battery and sleep handling:

```bash
git clone -b clara-face https://github.com/cwe6279/clara-esp32.git ~/esp/xiaozhi-esp32
cd ~/esp/xiaozhi-esp32
echo 'CONFIG_OTA_URL="http://<host address>:8030/xiaozhi/ota/"' >> sdkconfig.defaults
source ~/esp/esp-idf/export.sh
python scripts/build.py --wake-word custom waveshare/esp32-s3-touch-amoled-1.8-v2
```

`--wake-word custom` makes her wake to "Clara", "Clairuh", "hey Clara" and "hi Clara". Without
it the board answers to "Hi ESP". The phrase list is `CONFIG_CUSTOM_WAKE_WORD` in
`sdkconfig.defaults`. [SATELLITE_ESP32.md](SATELLITE_ESP32.md) explains how its spellings were
chosen.

**Another board:** clone upstream
[xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) instead and build for your board's name
from its `main/boards/` folder. Set the same `CONFIG_OTA_URL`. She won't have a face, and she
wakes to that firmware's stock wake word. An 8 MB board, like the XIAO ESP32S3 Sense, needs
its wake phrases written as phoneme letters. See the notes in the fork,
`tools/board-probes/xiao-esp32s3-sense/README.md`.

**Check:** the build ends without errors and leaves `build/merged-binary.bin`.

## 6. Flash the board

Let your user open the board's serial port without sudo. This only has to be done once:

```bash
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", MODE="0666"' | sudo tee /etc/udev/rules.d/99-espressif.rules
sudo udevadm control --reload-rules
```

Plug the board in. It should appear as `/dev/ttyACM0`. The first flash erases everything and
writes the whole image, including the wake-word model:

```bash
esptool --chip esp32s3 --port /dev/ttyACM0 erase-flash
esptool --chip esp32s3 --port /dev/ttyACM0 -b 921600 --after watchdog-reset \
    write-flash --flash-mode dio --flash-size 16MB --flash-freq 80m 0x0 build/merged-binary.bin
```

Use `--flash-size 8MB` for an 8 MB board. If esptool can't connect, put the board in download
mode. On the reference board: hold PWR until the screen goes dark, hold BOOT, tap PWR, then
release BOOT after two seconds.

**Check:** the board restarts and opens a Wi-Fi hotspot named `Xiaozhi-XXXX`.

## 7. Put the board on your Wi-Fi

1. On a phone, join the board's `Xiaozhi-XXXX` hotspot.
2. Open http://192.168.4.1 in the phone's browser.
3. Pick your 2.4 GHz network and enter its password.

The board saves the network and restarts onto it. The same page has an advanced section with
the server address. Use it if the board was built without `CONFIG_OTA_URL`, or if the host's
address has changed.

To change networks later, press BOOT during the first few seconds after power-on to bring the
setup page back.

Later firmware updates write only the app, so the saved Wi-Fi survives. See
[SATELLITE_ESP32.md](SATELLITE_ESP32.md), "Building and flashing".

**Check:** the hotspot disappears. The board can't reach Clara yet; the next step starts her.

## 8. First run

On the host, in the `clara-code` folder with the virtual environment active:

```bash
python tools/claude_box_relay.py &
python voice_loop.py --face clara --satellite --no-window --stt whisper --tts piper \
    --agent-url http://127.0.0.1:8032
```

The board connects to the host on port 8030. Fedora Workstation's firewall already allows it,
because its default zone opens ports 1025 and up. If your firewall is stricter, open the port:

```bash
sudo firewall-cmd --add-port=8030/tcp --permanent && sudo firewall-cmd --reload   # firewalld
sudo ufw allow 8030/tcp                                                            # ufw
```

The first start downloads the speech models, a few hundred MB, so give it a minute. With an
ElevenLabs key, use `--stt elevenlabs --tts elevenlabs` instead, which is faster.

**Check:** say "Clara" to the board and ask her something simple. Then give her a job: "Clara,
have Claude Code write a haiku about the weather into a file." She says she's on it, and tells
you when it's done. `clara-box` shows the job running.

## 9. Lock Clara to your board

Without this, any device on your network can connect to Clara. Open the control page at
http://localhost:8020, go to **Boards**, and press **Allow only this board**. Other devices are
refused from then on. A board you add later shows up under **Refused**; press **Allow** next to
it.

**Check:** the **Boards** tab lists your board as allowed.

## 10. Keep her running

Stop the two programs from step 8 (`Ctrl-C`, then `kill %1`), and install them as services that
start with the computer and restart if they stop:

```bash
services/install.sh            # add --speaker for a board that plays its speech over USB
systemctl --user start clara-relay clara-host
loginctl enable-linger "$USER"  # keep them running while you're logged out
```

Clara's options now live in `~/.config/clara-code/host.env`, on the `CLARA_ARGS=` line. After
changing them, run `systemctl --user restart clara-host`.

**Check:** reboot the host, then wake the board and talk. She answers without you starting
anything.

## Optional: a Raspberry Pi as Clara's voice host

A Pi Zero 2 W can sit by the board and run her hearing, conversation and voice, all in the
cloud. Claude Code's jobs still go to the box on the main computer.

**Getting the Pi onto Wi-Fi with no keyboard:** in Raspberry Pi Imager, open the advanced
options (the gear) before writing the SD card. Set the Wi-Fi name, password and country, a
hostname, and turn on SSH. The Pi joins your network on its first boot. Give it a DHCP
reservation too, and point the board's `CONFIG_OTA_URL` at the Pi instead of the main computer.

The rest is in [RPI_ZERO_2W.md](RPI_ZERO_2W.md): "Install", then "Satellite host", which covers
letting the relay accept jobs from the Pi with a shared token.

## If something doesn't work

- **The board never shows up as `/dev/ttyACM0`:** try a USB-C to USB-C cable, and check
  `lsusb` for `303a:1001`.
- **The board joins Wi-Fi but Clara never answers:** check that the host is running, that
  port 8030 is open, and that the OTA address matches the host. The board's setup page, under
  advanced, shows the address it is using.
- **She stops answering later:** press **Restart board** on the control page's **Boards**
  tab. If the board isn't connected, unplug it and plug it back in. More in
  [SATELLITE_ESP32.md](SATELLITE_ESP32.md#when-the-board-stops-responding).
- **Jobs fail with "No API key in the box":** run `clara-box-key` again (step 4).
- **Logs:** `journalctl --user -u clara-relay -f` follows the relay. Clara's conversations are in
  `logs/`, also on the control page's **Box & files** tab.
