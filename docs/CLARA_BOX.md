# Clara's box: a VM where Claude Code does her jobs

Clara hands work to Claude Code, and Claude Code runs inside its own virtual machine, not on
your computer. This page covers how the pieces connect, what the box can and cannot reach, how
to watch it, and how to build it. The files are in `box/`.

## How a job travels

```
you ──voice──> board ──> voice_loop.py (Clara) ──{{task …}}──> tools/claude_box_relay.py
                                                                      │ ssh, 127.0.0.1:2222
                                                              clara-box VM: ~/bin/cc-run
                                                                      │
                                                              claude -p (auto mode), tmux window cc-<id>
```

1. Clara decides a request is a job and writes `{{task …}}` (her `errands` list in
   `faces/clara/face.json` says what she can hand off).
2. `talker/errands.py` posts it to the relay and polls for the result.
3. The relay (`tools/claude_box_relay.py`, port 8032) runs `~/bin/cc-run` in the box over SSH.
   That starts Claude Code headless in a tmux window you can watch.
4. Claude Code's final answer comes back. Clara says it in her own words. If the answer starts
   with `QUESTION:`, she asks you, and your reply resumes the same Claude Code session.

While a job is out the board stays connected, so she can announce the result unprompted.

Run it:

```bash
python tools/claude_box_relay.py &
python voice_loop.py --face clara --satellite … --agent-url http://127.0.0.1:8032
```

Relay behaviour, all in the header of `tools/claude_box_relay.py`:

- **Follow-ups.** A job within 20 minutes of the last one continues that Claude Code
  conversation ("now add tests too"). Saying "new job" or "start fresh" starts a new one.
- **Model.** Haiku by default. Say "sonnet" or "opus" in the request to use that model.
- **Caps.** `CLARA_BOX_CLAUDE_JOB_USD` (default 1.00, passed as `--max-budget-usd`) and
  `CLARA_BOX_CLAUDE_MONTHLY_USD` (default 20). Jobs are refused once the month's total passes it.
- **Costs.** Each job's cost goes to `~/.local/share/clara-box/claude-costs.jsonl` on the host,
  where the box cannot edit it. Ask Clara "what has Claude Code cost?", or run
  `clara-box-costs [days]`. The Anthropic Console shows the real bill.
- **One at a time.** Jobs queue, so two never write to the same session.

## The API key

Claude Code in the box uses its own key, never yours.

- `clara-box-key` asks for the key without echoing it and writes it to `~/.config/anthropic-key`
  in the box. To rotate it, run the command again with the new key, then revoke the old one in
  the Console.
- **Give the key its own Console workspace with a monthly spend limit, before the first job.**
  Anything in the box can read the key, and a job misled by a web page could change the box's
  own files and the costs it reports. The Console limit is enforced by Anthropic, outside the
  box, so it is the one cap that always holds.

## Security model

- **A separate machine.** A KVM guest under your own libvirt (`qemu:///session`), so building
  and running it needs no root on the host. No shared folders, no host devices.
- **No privileges inside.** Claude Code runs as user `clara`: no sudo, root's password locked,
  SSH key restricted (`restrict,pty`: no port, agent or X11 forwarding). It can read, write and
  delete in its home (`/home/clara`) and `/tmp`, and nowhere else.
- **Internet, no LAN.** The guest's nftables rejects every private, link-local, CGNAT and
  multicast destination, and IPv6 is off. The box cannot reach the host, the router or other
  devices on your network. DNS is pinned to 1.1.1.1 / 9.9.9.9. The rules belong to root in the
  guest, so `clara` cannot change them.
- **Reachable from this host only.** SSH is forwarded on `127.0.0.1:2222`, and ports 8100 to
  8119 on `127.0.0.1` for pages the box serves. Nothing is open to the LAN.
- **What crosses back.** Text only: Claude Code's final answer. No files are copied to the host.

Owner access is a separate `admin` user (sudo) that accepts only your own SSH key:
`ssh -p 2222 admin@127.0.0.1`.

## The host side, and what is left to you

The box limits what Claude Code can touch. These limit who can give it work:

- **The relay takes local programs only.** It listens on `127.0.0.1` and refuses anything that
  looks like a web page's request (an `Origin` header, a form content type, a foreign `Host`),
  so a page open in your browser, including one the box serves, cannot start a job or read the
  task list. It will not listen beyond this machine unless `AGENT_RELAY_TOKEN` is set; the voice
  host sends the same variable as `X-Relay-Token`.
- **Files are owner-only.** `~/.local/share/clara-box` is mode 700: the VM disk holds the box's
  API key, and the relay's state holds what you asked for. Keep `.env` at 600 as well.
- **Answers are treated as untrusted text.** Claude Code may repeat what a web page told it.
  The relay removes Clara's own tool syntax from answers and caps their length, and only fixed
  model names and well-formed session ids reach the box's command line.

Known limits, by design:

- **Without an allow-list, anyone on your network can talk to Clara.** The board's port (8030)
  is open to the LAN so the board can reach it. Turn the allow-list on from the control page's
  **Boards** tab ("Allow only this board"), or set `SATELLITE_ALLOW` in `.env` to your board's
  Client-Id. Other devices are then refused, logged, and listed on that tab to allow or ignore. The id travels unencrypted on your network, so
  this keeps out other devices, not someone able to capture your Wi-Fi traffic. A MAC address
  (Device-Id) also works but is easier to copy.
- **The control page has no password.** It stays on this machine unless you pass
  `--web-host 0.0.0.0`, which opens settings, the camera snapshot, Wi-Fi joining and the board
  allow-list to the LAN. It refuses requests made by other web pages (form posts, other
  origins, a foreign `Host`), so a site open in your browser cannot use it.
- **A web page can mislead Claude Code.** It runs in auto mode and reads the web, so text on a
  page can steer it. Inside the box that can waste money, read the box's API key or change the
  box's own files (including `cc-run` and the costs it reports). It cannot reach your computer
  or your network. This is why the key gets its own Console workspace and spend limit: that
  limit holds even if everything in the box is compromised, and the ledger's caps do not.
- **Pages the box serves run in your browser** at `localhost:8100` to `8119`. Treat them like
  any page from the internet.

## Watching it

`clara-box` (a launcher in `~/.local/bin`, with a "Clara Box" dock icon) starts the VM if it is
off and attaches tmux session `clara`:

- window `shell`: a shell as `clara`, for you;
- window `activity`: one line per job as it starts;
- windows `cc-<id>`: a Claude Code job, live (text and tool calls; closes 10 minutes after it ends);
- windows `app-<name>`: servers Claude Code started.

`Ctrl-b 0/1/n` switches windows, `Ctrl-b d` detaches (the session keeps running).

## Pages the box serves

A server started in the box on ports 8100 to 8119 is at `http://localhost:PORT` on the host.
`box/CLAUDE.md` tells Claude Code to use that range, to run each server in its own tmux window,
and to give the localhost address. Servers stop when the box restarts.

## What is installed

Debian 13 (genericcloud image), 4 vCPU, 4 GB RAM, 40 GB thin disk (about 3.5 GB used on the host
after setup). Change the CPU and memory in `box/clara-box.xml` to suit your host; 2 CPUs and
2 GB is a reasonable floor for Claude Code with a headless browser, though that is untested. Claude Code at `~/.local/bin/claude`. In `~/venv`: Playwright with Chromium
(headless shell), pandas, openpyxl, pypdf, pymupdf, requests, beautifulsoup4, lxml, ddgs (web
search). `clara` can `pip install` more into `~/venv`, but cannot `apt install`: use `admin`.

## Building it

Host needs: libvirt with the QEMU driver, `passt`, `genisoimage`, an SSH key in
`~/.ssh/id_ed25519`. The VM lives in `~/.local/share/clara-box/`.

```bash
D=~/.local/share/clara-box; S=box; mkdir -p $D/keys $D/seed
( cd $D && curl -LO https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2 )
#   check it against SHA512SUMS from the same folder
ssh-keygen -t ed25519 -N "" -C clara-box-agent -f $D/keys/clara
qemu-img create -f qcow2 -F qcow2 -b $D/debian-13-genericcloud-amd64.qcow2 $D/clara-box.qcow2 40G
sed -e "s#@ADMIN_KEY@#$(cat ~/.ssh/id_ed25519.pub)#" -e "s#@AGENT_KEY@#$(cat $D/keys/clara.pub)#" \
    $S/user-data > $D/seed/user-data
printf 'instance-id: clara-box-1\nlocal-hostname: clara-box\n' > $D/seed/meta-data
genisoimage -output $D/seed.iso -volid cidata -joliet -rock $D/seed/user-data $D/seed/meta-data
sed "s#@DIR@#$D#g" $S/clara-box.xml > $D/clara-box.xml
virsh -c qemu:///session define $D/clara-box.xml && virsh -c qemu:///session start clara-box
```

After cloud-init finishes (`grep finished $D/console.log`), as `admin`:

```bash
echo 'network: {config: disabled}' | sudo tee /etc/cloud/cloud.cfg.d/99-no-net.cfg
sudo tee /etc/netplan/50-cloud-init.yaml <<'NET'
network:
  version: 2
  ethernets:
    nic:
      match: {name: "en*"}
      dhcp4: true
      dhcp4-overrides: {use-dns: false}
      nameservers: {addresses: [1.1.1.1, 9.9.9.9]}
NET
sudo chmod 600 /etc/netplan/50-cloud-init.yaml && sudo netplan apply
sudo mkdir -p /workspace && sudo ln -sfn /home/clara/work /workspace/agent
sudo loginctl enable-linger clara
python3 -m venv /tmp/pw && /tmp/pw/bin/pip install playwright && sudo /tmp/pw/bin/playwright install-deps chromium
```

Copy into the box as `clara` (`scp -P 2222 -i $D/keys/clara …`): `cc-run` to `~/bin/`,
`tmux.conf` to `~/.tmux.conf`, `clara-tmux.service` to `~/.config/systemd/user/`, and
`CLAUDE.md` to `~/.claude/CLAUDE.md`. Then, as `clara`:

```bash
mkdir -p ~/work && python3 -m venv ~/venv
~/venv/bin/pip install playwright pandas openpyxl pypdf pymupdf requests beautifulsoup4 lxml ddgs
~/venv/bin/playwright install chromium
curl -fsSL https://claude.ai/install.sh | bash
systemctl --user enable --now clara-tmux
```

On the host, copy `clara-box-launcher` to `~/.local/bin/clara-box`, and `clara-box-key` and
`clara-box-costs` to `~/.local/bin/`; make them executable. Copy `clara-box.desktop` (with
`@HOME@` replaced) to `~/.local/share/applications/`. Then run `clara-box-key`.

## Checks

As `clara`, these should all fail: `sudo -n true`, `touch /etc/x`, a TCP connect to a
`192.168.x.x` address or to the host. `curl https://deb.debian.org` should succeed.
`tests/test_claude_box_relay.py` covers the relay without a VM.
