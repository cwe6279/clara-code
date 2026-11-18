"""The Settings tab: .env edited in place, secrets never sent to the page, read-only on the LAN."""
import json
import os
import urllib.request

import pytest

from talker.settings_store import SettingsStore
from talker.web_panel import WebPanel

SECRET = "sk-ant-api03-SECRETVALUE-do-not-leak"


def _store(tmp_path, writable=True):
    env = tmp_path / ".env"
    env.write_text(f"# Clara's keys\nANTHROPIC_API_KEY={SECRET}\nexport PIPER_VOICE=en_US-a\n\nCUSTOM_THING=1\nMY_SERVICE_TOKEN=abc\n")
    os.chmod(env, 0o644)
    return SettingsStore(str(env), str(tmp_path / "cfg" / "host.env"), writable=writable), env


def test_secrets_are_never_returned_and_edits_keep_the_file(tmp_path):
    st, env = _store(tmp_path)
    v = st.view()
    assert SECRET not in json.dumps(v) and "abc" not in json.dumps(v)
    rows = {r["key"]: r for r in v["settings"]}
    assert rows["ANTHROPIC_API_KEY"]["set"] and rows["ANTHROPIC_API_KEY"]["value"] is None
    assert rows["MY_SERVICE_TOKEN"]["kind"] == "secret" and rows["CUSTOM_THING"]["value"] == "1"
    st.set("PIPER_VOICE", "en_US-b")
    st.set("CLARA_BOX_CLAUDE_MONTHLY_USD", "30")
    text = env.read_text()
    assert "# Clara's keys" in text and f"ANTHROPIC_API_KEY={SECRET}" in text and "PIPER_VOICE=en_US-b" in text
    assert "en_US-a" not in text and text.rstrip().endswith("CLARA_BOX_CLAUDE_MONTHLY_USD=30")
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert st.view()["pending"] == ["clara-host", "clara-relay"]
    st.clear("CUSTOM_THING")
    assert "CUSTOM_THING" not in env.read_text()


def test_bad_values_and_read_only(tmp_path):
    st, env = _store(tmp_path)
    for key, value in [("lower", "x"), ("PIPER_VOICE", "a\nEVIL=1"), ("ANTHROPIC_API_KEY", ""),
                       ("CLARA_BOX_CLAUDE_JOB_USD", "-1")]:
        with pytest.raises(ValueError):
            st.set(key, value)
    assert "EVIL" not in env.read_text()
    st.set_host_args("--face clara --satellite")
    assert "CLARA_ARGS=--face clara --satellite" in (tmp_path / "cfg" / "host.env").read_text()
    ro, _ = _store(tmp_path, writable=False)
    with pytest.raises(PermissionError):
        ro.set("PIPER_VOICE", "x")
    with pytest.raises(ValueError):
        st.service("clara-host", False)                # only the speaker can be switched


def test_the_page_never_carries_a_secret(tmp_path):
    st, _ = _store(tmp_path)
    panel = WebPanel(port=0, host="127.0.0.1")
    panel.settings = st
    panel.start()
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{panel.port}/api/settings", timeout=5).read()
        assert SECRET.encode() not in body and json.loads(body)["writable"]
        req = urllib.request.Request(f"http://127.0.0.1:{panel.port}/api/settings/set",
                                     data=json.dumps({"key": "PIPER_VOICE", "value": "en_GB-x"}).encode(),
                                     headers={"Content-Type": "application/json"})
        assert "saved" in json.loads(urllib.request.urlopen(req, timeout=5).read())["message"]
    finally:
        panel.stop()


def test_an_older_name_counts_as_set_and_saving_tidies_it(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FISH_AUDIO_KEY=fish-secret\n")
    st = SettingsStore(str(env), str(tmp_path / "h.env"))
    rows = {r["key"]: r for r in st.view()["settings"]}
    assert rows["FISH_AUDIO_API_KEY"]["set"] and rows["FISH_AUDIO_API_KEY"]["alias"] == "FISH_AUDIO_KEY"
    assert "FISH_AUDIO_KEY" not in rows and "fish-secret" not in json.dumps(st.view())
    st.set("FISH_AUDIO_API_KEY", "new-fish")
    assert env.read_text() == "FISH_AUDIO_API_KEY=new-fish\n"
