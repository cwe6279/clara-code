"""Clara's box is still contained: box/containment-probe, run inside the box as clara, must print
exactly box/containment-expected.txt, and the host side (passt, forwarded ports, QEMU's
confinement) must still be as set up. Read-only. Skipped when the box is not running.

Each line pins a hole closed on purpose (docs/CLARA_BOX.md, "Security model"). To change a
line, change the box first, then the expected file, and say why in the commit message.
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOX = Path(os.environ.get("CLARA_BOX_DIR", Path.home() / ".local/share/clara-box"))
SSH = ["ssh", "-q", "-i", str(BOX / "keys" / "clara"), "-p", "2222",
       "-o", f"UserKnownHostsFile={BOX / 'known_hosts'}", "-o", "StrictHostKeyChecking=yes",
       "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "clara@127.0.0.1"]


def _box_up() -> bool:
    if not (BOX / "keys" / "clara").exists():
        return False
    try:
        return subprocess.run(SSH + ["true"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _box_up(), reason="Clara's box is not running here")


def _pairs(text: str) -> dict:
    out = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            k, _, v = ln.partition(" ")
            out[k] = v
    return out


def test_probe_inside_the_box_matches_the_expected_file():
    p = subprocess.run(SSH + ["/usr/local/bin/containment-probe"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    got = _pairs(p.stdout)
    print("box kernel:", got.pop("kernel", "?"))
    want = _pairs((ROOT / "box" / "containment-expected.txt").read_text())
    diff = {k: (want.get(k), got.get(k)) for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)}
    assert not diff, "check: (expected, found) " + repr(diff)


def test_installed_probe_is_the_repo_copy():
    p = subprocess.run(SSH + ["cat", "/usr/local/bin/containment-probe"], capture_output=True, text=True, timeout=30)
    assert p.stdout == (ROOT / "box" / "containment-probe").read_text(), \
        "the box's probe differs from box/containment-probe: reinstall it as admin"


def test_installed_runner_is_the_repo_copy():
    p = subprocess.run(SSH + ["cat", "/usr/local/bin/cc-run"], capture_output=True, text=True, timeout=30)
    assert p.stdout == (ROOT / "box" / "cc-run").read_text(), \
        "the box's runner differs from box/cc-run: reinstall it as admin"


def _procs():
    p = subprocess.run(["ps", "-eo", "label=,args="], capture_output=True, text=True)
    return p.stdout.splitlines()


def test_box_network_does_not_map_the_gateway_to_this_host():
    pid = subprocess.run(["systemctl", "--user", "show", "clara-box-net", "-p", "MainPID", "--value"],
                         capture_output=True, text=True).stdout.strip()
    assert pid and pid != "0", "clara-box-net.service (the box's passt) is not running"
    args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    assert b"--no-map-gw" in args, args
    # and libvirt's own passt, which maps the gateway, is not in use
    assert not [ln for ln in _procs() if "libvirt/qemu/run/passt" in ln and "<defunct>" not in ln]


def test_forwarded_ports_listen_on_localhost_only():
    out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
    ports = {2222, *range(8100, 8120)}
    for ln in out.splitlines():
        local = ln.split()[3]
        addr, _, port = local.rpartition(":")
        if port.isdigit() and int(port) in ports:
            assert addr in ("127.0.0.1", "[::1]"), f"box port open beyond this machine: {local}"


def test_qemu_is_selinux_confined():
    if subprocess.run(["getenforce"], capture_output=True, text=True).stdout.strip() != "Enforcing":
        pytest.skip("SELinux is not enforcing on this host")
    qemu = [ln for ln in _procs() if "qemu-system" in ln and "guest=clara-box" in ln]
    assert qemu and all(":svirt_t:" in ln for ln in qemu), qemu
