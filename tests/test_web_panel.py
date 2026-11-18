import os
"""The control page: state, live tunables, actions, reference, all over plain HTTP."""
import argparse
import json
import urllib.request

from talker.web_panel import WebPanel, describe_parser


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_panel_serves_state_tunables_and_actions():
    box = {"silence": 600, "spoken": []}
    panel = WebPanel(port=0, host="127.0.0.1")
    panel.status_fn = lambda: {"face": "pumpkin", "mic_rms": 42}
    panel.tunable("silence_ms", lambda: box["silence"], lambda v: box.__setitem__("silence", v),
                  "pause that ends a turn", kind="int", unit="ms", lo=150, hi=2000, flag="--silence-ms")
    panel.action("speak", lambda t: (box["spoken"].append(t), "speaking")[1], "say it", takes_text=True)
    p = argparse.ArgumentParser()
    p.add_argument("--silence-ms", type=int, default=600, help="pause (default %(default)s)")
    panel.parser, panel.args = p, p.parse_args(["--silence-ms", "450"])
    url = panel.start()
    assert url and panel.port > 0
    base = f"http://127.0.0.1:{panel.port}"
    try:
        code, ctype, body = _get(base + "/")
        assert code == 200 and "text/html" in ctype and b"Talker control" in body
        st = json.loads(_get(base + "/api/state")[2])
        assert st["status"]["face"] == "pumpkin" and st["tunables"][0]["value"] == 600
        assert st["tunables"][0]["flag"] == "--silence-ms" and "pause" in st["tunables"][0]["help"]
        r = _post(base + "/api/set", {"name": "silence_ms", "value": "100"})     # clamped to lo
        assert r["value"] == 150 and box["silence"] == 150
        r = _post(base + "/api/action", {"name": "speak", "text": "hello"})
        assert r["message"] == "speaking" and box["spoken"] == ["hello"]
        ref = json.loads(_get(base + "/api/reference")[2])
        assert ref["flags"][0]["flags"] == "--silence-ms" and ref["flags"][0]["value"] == "450"
        assert "default 600" in ref["flags"][0]["help"]
        assert _post(base + "/api/set", {"name": "nope", "value": 1})["error"]
        assert any(e["kind"] == "panel" for e in json.loads(_get(base + "/api/state")[2])["events"])
    finally:
        panel.stop()


def test_agent_tab_sets_tests_and_lists_tasks():
    from talker.errands import AgentControl, ErrandRunner
    saved = []
    health = lambda url, *a, **k: {"ok": True, "running": 0} if url.endswith("/health") else {"id": "x"}
    runner = ErrandRunner("", fetch=health)
    panel = WebPanel(port=0, host="127.0.0.1")
    base = None
    try:
        panel.start()
        base = f"http://127.0.0.1:{panel.port}"
        assert "error" in json.loads(_get(base + "/api/agent")[2])          # a face without errands
        assert json.loads(_get(base + "/api/state")[2])["agent"] is False
        panel.agent = AgentControl(runner, can="research", source="none", save=saved.append)
        info = json.loads(_get(base + "/api/agent")[2])
        assert info["url"] == "" and info["can"] == "research" and info["tasks"] == []
        assert _post(base + "/api/agent/test", {"url": "http://agentbox:8030"})["ok"] is True
        assert "error" in _post(base + "/api/agent/set", {"url": "agentbox:8030"})
        r = _post(base + "/api/agent/set", {"url": "http://agentbox:8030"})
        assert r["url"] == "http://agentbox:8030" and runner.url == r["url"] and saved == [r["url"]]
        runner.submit("find a projector")
        info = json.loads(_get(base + "/api/agent")[2])
        assert info["open"] == 1 and info["tasks"][0]["task"] == "find a projector"
        assert any(e["kind"] == "agent" for e in json.loads(_get(base + "/api/state")[2])["events"])
        assert b'data-t="agent"' in _get(base + "/")[2]                     # the tab is on the page
    finally:
        panel.stop()


def test_a_tunable_with_options_is_a_dropdown_and_refuses_other_values():
    box = {"awb": "Auto"}
    panel = WebPanel(port=0, host="127.0.0.1")
    panel.tunable("camera_awb", lambda: box["awb"], lambda v: box.__setitem__("awb", v), "white balance",
                  kind="str", options=["Auto", "Daylight", "Cloudy"])
    panel.start()
    base = f"http://127.0.0.1:{panel.port}"
    try:
        st = json.loads(_get(base + "/api/state")[2])
        assert st["tunables"][0]["options"] == ["Auto", "Daylight", "Cloudy"]
        assert _post(base + "/api/set", {"name": "camera_awb", "value": "daylight"})["value"] == "Daylight"
        assert "must be one of" in _post(base + "/api/set", {"name": "camera_awb", "value": "purple"})["error"]
        assert box["awb"] == "Daylight"
        assert b"<select" in _get(base + "/")[2]
    finally:
        panel.stop()


def test_describe_parser_handles_percent_and_bools():
    p = argparse.ArgumentParser()
    p.add_argument("--vision-change", type=float, default=0.035, help="3.5%% change (default %(default)s)")
    p.add_argument("--barge-in", action="store_true", help="interrupt")
    rows = describe_parser(p, p.parse_args([]))
    assert rows[0]["help"] == "3.5% change (default 0.035)" and rows[1]["value"] == "False"


def test_nmcli_fields_unescape_colons():
    from talker.web_panel import WifiControl
    assert WifiControl._fields("no:HomeNet:100:WPA2 WPA3") == ["no", "HomeNet", "100", "WPA2 WPA3"]
    assert WifiControl._fields(r"yes:Cafe\:Wifi:80:WPA2") == ["yes", "Cafe:Wifi", "80", "WPA2"]


def test_a_face_can_name_its_own_brain_per_llm():
    from talker.face_asset_loader import FaceManifest
    m = FaceManifest.from_dict({"models": {"Claude": "claude-haiku-4-5", "ollama": "qwen3:8b"}})
    assert m.models == {"claude": "claude-haiku-4-5", "ollama": "qwen3:8b"}
    assert m.models.get("claude") == "claude-haiku-4-5"
    assert m.models.get("openai") is None            # unset brain falls through to the default
    assert FaceManifest.from_dict({}).models == {}


def test_a_face_name_is_found_whatever_its_case(capsys):
    from talker.app import resolve_face_dir
    assert resolve_face_dir("theboard", None).endswith(os.path.join("faces", "theboard"))
    assert resolve_face_dir("TheBoard", None).endswith(os.path.join("faces", "theboard"))
    assert resolve_face_dir("CLARA", None).endswith(os.path.join("faces", "clara"))
    capsys.readouterr()
    assert resolve_face_dir("no-such-face", None) is None
    out = capsys.readouterr().out
    assert "no face called 'no-such-face'" in out and "theboard" in out    # lists what exists


def test_other_pages_cannot_post_or_read():
    """No password on this page, so a site open in the browser must not be able to use it."""
    import http.client
    hits = []
    panel = WebPanel(port=0, host="127.0.0.1")
    panel.action("speak", lambda t: (hits.append(t), "ok")[1], "say it", takes_text=True)
    panel.start()

    def call(method, path, headers, body=None):
        c = http.client.HTTPConnection("127.0.0.1", panel.port, timeout=5)
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status
    b = json.dumps({"name": "speak", "text": "hi"})
    try:
        assert call("POST", "/api/action", {"Content-Type": "text/plain"}, b) == 403            # a form post
        assert call("POST", "/api/action", {"Content-Type": "application/json",
                                            "Origin": "http://evil.example"}, b) == 403
        assert call("GET", "/api/state", {"Host": "evil.example"}) == 403                        # DNS rebinding
        assert not hits
        assert call("POST", "/api/action", {"Content-Type": "application/json",
                                            "Origin": f"http://127.0.0.1:{panel.port}"}, b) == 200   # the page itself
        assert hits == ["hi"]
    finally:
        panel.stop()


def test_boards_tab_allows_a_refused_board_and_saves_it(tmp_path):
    from talker.satellite import SatelliteServer
    saved = []
    sat = SatelliteServer(host="127.0.0.1", port=0, allow=["aaaa-1111"], on_event=lambda k, t: None)
    sat.locked_ids = {"aaaa-1111"}
    sat.on_allow_change = saved.append
    sat.refused["bbbb-2222"] = {"client": "bbbb-2222", "mac": "de:ad", "ip": "10.0.0.9", "seen": 1.0, "count": 3}
    panel = WebPanel(port=0, host="127.0.0.1")
    panel.boards = sat
    panel.start()
    base = f"http://127.0.0.1:{panel.port}"
    try:
        r = json.loads(_get(base + "/api/boards")[2])
        assert not r["open"] and r["refused"][0]["id"] == "bbbb-2222" and r["allowed"][0]["locked"]
        r = _post(base + "/api/boards/allow", {"id": "bbbb-2222"})
        assert {a["id"] for a in r["allowed"]} == {"aaaa-1111", "bbbb-2222"} and not r["refused"]
        assert saved[-1] == ["bbbb-2222"]                       # only the page-added one is saved
        assert "error" in _post(base + "/api/boards/remove", {"id": "aaaa-1111"})   # from .env: not here
        assert "error" in _post(base + "/api/boards/allow", {"id": "<script>"})
        r = _post(base + "/api/boards/remove", {"id": "bbbb-2222"})
        assert [a["id"] for a in r["allowed"]] == ["aaaa-1111"] and saved[-1] == []
    finally:
        panel.stop()


def test_box_tab_asks_the_relay(monkeypatch):
    import talker.errands as errands
    seen = []

    def fake_http(url, method="GET", body=None, timeout=5.0):
        seen.append((url, method, body))
        if method == "GET":
            return {"apps": [], "strays": [], "library": [{"name": "old"}], "settings": {}}
        return {"message": "restarted old"}
    monkeypatch.setattr(errands, "_http", fake_http)
    panel = WebPanel(port=0, host="127.0.0.1")
    base_none = panel._box("GET", "/box/apps")[0]
    assert "no Claude Code relay" in base_none["error"]
    panel.box_url = lambda: "http://127.0.0.1:8032"
    panel.start()
    base = f"http://127.0.0.1:{panel.port}"
    try:
        assert json.loads(_get(base + "/api/box")[2])["library"][0]["name"] == "old"
        assert _post(base + "/api/box/restart", {"name": "old"})["message"] == "restarted old"
        assert seen[-1] == ("http://127.0.0.1:8032/box/apps/restart", "POST", {"name": "old"})
    finally:
        panel.stop()
