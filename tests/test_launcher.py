"""The launcher (app/flsync.py, docs/phase3.md "The launcher"):

  * invite codes: round trip, typos caught, forgiving reading;
  * which addresses go into an invite, and which one a joiner keeps;
  * UPnP against a fake router (SSDP and SOAP on 127.0.0.1);
  * the settings file and the checklist's diagnoses;
  * launchers running for real against each other, each with a minimal FL on
    a fake MIDI port and the page's API: hosting, joining, versions, notes,
    the pen, leaving, and who may talk to the page at all;
  * the program itself, started the way the shortcut starts it.

Nothing here touches the real computer's router, MIDI ports, FL, %LOCALAPPDATA%
or port 31415: the launchers get a fake winsys.System, temporary folders and
free ports.

    python -m unittest discover -s tests -p "test_launcher.py" -v
"""

import asyncio
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fake_fl import ROOT, FakeFL
from harness import FAST, LoopThread, RawClient, RelayServer, free_port, pump
from test_snapshots import FP_A, FP_B, NOTE, WireFL, make_project, save

sys.path.insert(0, os.path.join(ROOT, "app"))
import flsync  # noqa: E402  (puts app/, bridge/ and relay/ on the path)
import diagnose  # noqa: E402
import invite  # noqa: E402
import upnp  # noqa: E402
import webui  # noqa: E402
import winsys  # noqa: E402

PUBLIC = "81.2.3.4"  # a public address (the test ranges like 203.0.113.x aren't "global")
FL25, FL26 = "25.2.4.4960", "26.1.7.5512"


# --- a fake computer and a fake MIDI port --------------------------------------------------

class FakeSystem:
    """What the launcher asks the computer, answered by the test."""

    def __init__(self):
        self.procs = []                               # winsys.Process
        self.ports = (["FLSync"], ["FLSync"])         # (inputs, outputs)
        self.settings = None                          # FL's saved MIDI settings
        self.settings_asked = None                    # for which FL version they were last asked
        self.needs = {}                               # FL major -> its settings lack FLSync
        self.configured = []                          # FL majors set up by the launcher
        self.addresses = ["127.0.0.1"]
        self.default = None
        self.public = None
        self.lookups = 0
        self.opened = []
        self.browsed = []
        self.on_open = None

    def processes(self, names=(winsys.LOOPMIDI,) + winsys.FL_PROGRAMS):
        return None if self.procs is None else [p for p in self.procs if p.name in names]

    def midi_ports(self):
        return self.ports

    def fl_midi_settings(self, port, major=None):
        self.settings_asked = major
        return self.settings

    def fl_setup_needed(self, port):
        return dict(self.needs)

    def configure_fl(self, major):
        self.configured.append(major)
        self.needs[major] = False
        return 10

    def local_addresses(self):
        return list(self.addresses)

    def default_address(self):
        return self.default

    def public_address(self):
        self.lookups += 1
        return self.public

    def open_file(self, path):
        self.opened.append(path)
        if self.on_open:
            self.on_open(path)

    def open_browser(self, url):
        self.browsed.append(url)

    def user_name(self):
        return "Testare"


def fake_midi(fl):
    """bridge.run_midi for the tests: the launcher's bridge and `fl` share one
    fake loopMIDI port, where everything either side sends reaches both."""

    async def run_midi(bridge, to_fl="FLSync", from_fl=None, logger=print):
        def port(message):
            fl.from_bridge.put(message)
            bridge.incoming.append(message)

        for key, value in FAST.items():
            setattr(bridge, key, value)
        bridge.send_to_fl = port
        fl.to_bridge = port
        bridge.start()
        logger("MIDI-porten är öppen, väntar på FL. Avsluta med Ctrl+C")
        try:
            await bridge.pump()
        finally:
            fl.to_bridge = None
            bridge.close()

    return run_midi


def places(folder, project=None):
    """Every place on disk a bridge uses, inside `folder`; never the real FL's
    recent files or window title (a real FL may run meanwhile), and a version
    the bridge opens never reaches Windows (it would start FL Studio)."""
    return {"mru": lambda: project, "mru_all": lambda: [], "fl_title": lambda: None,
            "fl_open": lambda: False, "opener": lambda path: None,
            "cache_dir": os.path.join(folder, "cache"),
            "projects_dir": os.path.join(folder, "Projects", "FLSync"), "notes_dir": os.path.join(folder, "notes"),
            "plugin_db": os.path.join(folder, "Plugin database", "Installed"),
            "fl_dirs": {"factory_dir": os.path.join(folder, "FL Studio"), "user_dir": os.path.join(folder, "Image-Line"),
                        "downloads_dir": os.path.join(folder, "Image-Line", "Downloads"),
                        "home_dir": os.path.join(folder, "home")}}


def hello(fl, flv=None):
    msg = {"t": "hello", "proto": 2, "app": "fl", "name": fl.name, "api": 40, "fp": fl.fp}
    if flv:
        msg["flv"] = flv
    fl.send(msg)


class Launcher:
    """A launcher on the test's event loop, with a WireFL on its MIDI port."""

    def __init__(self, loop, folder, name, fp=FP_A, project=None, system=None, start=True, fl=None, **options):
        self.loop = loop
        self.folder = folder
        self.fl = fl or WireFL(name, fp)
        self.system = system or FakeSystem()
        options.setdefault("mapper", False)
        options.setdefault("ip_lookup", False)
        self.app = flsync.App(data_dir=os.path.join(folder, "data"), system=self.system, name=name, bind="127.0.0.1",
                              bridge_options=places(folder, project), run_midi=fake_midi(self.fl), echo=False,
                              probe_timeout=1.0, **options)
        self.app.look_interval = 0.1
        if start:
            self.loop.run(self.app.start())
            deadline = time.monotonic() + 5
            while self.fl.to_bridge is None and time.monotonic() < deadline:
                time.sleep(0.005)

    @property
    def port(self):
        return self.app.guard.port

    def request(self, method, path, body=None, headers=None, token=True):
        """(status, answer) of a request to the launcher's page, as its own page makes them."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        sent = {"Host": "127.0.0.1:%d" % self.port}
        if token:
            sent[webui.TOKEN_HEADER] = self.app.token
        data = None
        if body is not None:
            sent["Content-Type"] = "application/json"
            data = json.dumps(body)
        sent.update(headers or {})
        try:
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            raw, status, self.last_headers = response.read(), response.status, dict(response.getheaders())
        finally:
            connection.close()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode("utf-8")

    def status(self):
        status, answer = self.request("GET", "/api/status")
        assert status == 200, answer
        return answer

    def post(self, action, **body):
        return self.request("POST", "/api/" + action, body)

    def call(self, function, *args):
        async def run():
            return function(*args)

        return self.loop.run(run())

    def stop(self):
        self.loop.run(self.app.close(), timeout=30)


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.loop = LoopThread()
        self.loop_errors = []
        self.loop.loop.set_exception_handler(lambda loop, context: self.loop_errors.append(context))
        self.tmp = tempfile.mkdtemp(prefix="flsync-launcher-")
        self.launchers, self.clients, self.relays = [], [], []

    def tearDown(self):
        for launcher in reversed(self.launchers):
            launcher.stop()
            if isinstance(launcher.fl, FakeFL):
                launcher.fl.close()
        for relay in self.relays:
            relay.stop()
        for client in self.clients:
            client.close()
        self.loop.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual([c.get("message") for c in self.loop_errors], [])
        for launcher in self.launchers:
            self.assertEqual([line["text"] for line in launcher.app.lines if line["text"].startswith("fel")], [])

    def launcher(self, name, **options):
        folder = os.path.join(self.tmp, name)
        os.makedirs(folder, exist_ok=True)
        launcher = Launcher(self.loop, folder, name, **options)
        self.launchers.append(launcher)
        return launcher

    def wait_for(self, condition, timeout=5.0, why="tog för lång tid"):
        self.assertTrue(pump([l.fl for l in self.launchers], condition, timeout), why)

    def settle(self, seconds=0.3):
        pump([l.fl for l in self.launchers], lambda: False, seconds)

    def host(self, launcher, flv=FL25, **body):
        """Start hosting and get `launcher`'s FL into the session; returns the invite."""
        status, answer = launcher.post("host", port=body.pop("port", free_port()), **body)
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.wait_for(lambda: launcher.app.invite is not None, why="ingen inbjudan")
        hello(launcher.fl, flv)
        self.wait_for(lambda: launcher.app.bridge.in_session(), why="värdens FL kom inte med")
        return launcher.app.invite

    def join(self, launcher, code, flv=FL26):
        status, answer = launcher.post("join", code=code)
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        hello(launcher.fl, flv)
        self.wait_for(lambda: launcher.app.bridge.in_session(), why="gästens FL kom inte med")

    def raw(self, port, name, password, **hello_fields):
        client = RawClient(port, name, "fl", pw=password, **hello_fields)
        self.clients.append(client)
        return client


# --- invite codes ---------------------------------------------------------------------------

PASSWORD = bytes.fromhex("0123456789abcdef")


class InviteTest(unittest.TestCase):
    CASES = [(["192.168.1.20"], 31415), (["192.168.1.20", PUBLIC], 31415),
             (["10.0.0.5", "192.168.0.3", "100.101.102.103", PUBLIC], 40000), (["127.0.0.1"], 1)]

    def test_round_trip(self):
        for addresses, port in self.CASES:
            with self.subTest(addresses=addresses, port=port):
                code = invite.encode(addresses, port, PASSWORD)
                found = invite.decode(code)
                self.assertEqual((found.addresses, found.port, found.password), (addresses, port, PASSWORD.hex()))
                self.assertEqual(found.code, code)

    def test_the_code_reads_well(self):
        code = invite.encode(["192.168.1.20", PUBLIC], 31415, PASSWORD)
        self.assertRegex(code, r"^FLS1(-[0-9A-HJKMNP-TV-Z]{4})+(-[0-9A-HJKMNP-TV-Z]{1,4})?$")
        self.assertNotRegex(code[4:], "[ILOU]")  # nothing that reads like another character
        self.assertLessEqual(len(code), 45)
        # The default port isn't in the code: a custom one makes it longer.
        self.assertGreater(len(invite.encode(["192.168.1.20"], 40000, PASSWORD)),
                           len(invite.encode(["192.168.1.20"], 31415, PASSWORD)))

    def test_every_typo_is_caught(self):
        code = invite.encode(["192.168.1.20", PUBLIC], 31415, PASSWORD)
        chars = code[5:].replace("-", "")
        for i, ch in enumerate(chars):
            for other in invite.ALPHABET:
                if other != ch:
                    with self.subTest(position=i, char=other):
                        with self.assertRaises(invite.InviteError):
                            invite.decode("FLS1" + chars[:i] + other + chars[i + 1:])
        for i in range(len(chars) - 1):  # two neighbours swapped
            if chars[i] != chars[i + 1]:
                with self.assertRaises(invite.InviteError):
                    invite.decode("FLS1" + chars[:i] + chars[i + 1] + chars[i] + chars[i + 2:])
        for i in range(len(chars)):  # one left out
            with self.assertRaises(invite.InviteError):
                invite.decode("FLS1" + chars[:i] + chars[i + 1:])

    def test_reading_forgives_what_chat_apps_and_people_do(self):
        code = invite.encode(["192.168.1.20", PUBLIC], 31415, PASSWORD)
        chars = code[5:].replace("-", "")
        spoken = chars.replace("0", "O").replace("1", "l")  # read aloud and typed in
        for text in (code, code.lower(), code.replace("-", " "), code.replace("-", ""), "FLS1" + chars,
                     code.replace("-", "\u2013"), code.replace("-", " \u2014 "), " \n%s\n " % code,
                     "Här är koden: %s /Anna" % code, "`%s`" % code, '"%s"' % code, "**%s**" % code,
                     chars, "fls1 " + spoken.lower(), "".join(chr(ord(c) + 0xFEE0) for c in code.replace("-", ""))):
            with self.subTest(text=text):
                found = invite.decode(text)
                self.assertEqual((found.addresses, found.port), (["192.168.1.20", PUBLIC], 31415))

    def test_what_is_wrong_is_said(self):
        code = invite.encode(["192.168.1.20", PUBLIC], 31415, PASSWORD)
        for text, words in (("", "Klistra in"), ("hej", "ser inte ut som"), ("FLS2-ABCD-EFGH", "annan version"),
                            (code[:-6], "fattas"), (code[:-1] + ("0" if code[-1] != "0" else "2"), "stämmer inte"),
                            (code[:12] + "U" + code[13:], "tecknet U")):
            with self.subTest(text=text):
                with self.assertRaises(invite.InviteError) as caught:
                    invite.decode(text)
                self.assertIn(words, str(caught.exception))

    def test_a_password_is_random_and_fits(self):
        a, b = invite.new_password(), invite.new_password()
        self.assertNotEqual(a, b)
        self.assertEqual(len(bytes.fromhex(a)), invite.PASSWORD_BYTES)


class AddressTest(unittest.TestCase):
    def test_kinds(self):
        for address, kind in (("192.168.1.20", "lan"), ("10.1.2.3", "lan"), ("172.20.0.1", "lan"),
                              ("100.101.102.103", "tailscale"), (PUBLIC, "internet"), ("127.0.0.1", "local"),
                              ("169.254.3.4", "other"), ("203.0.113.7", "other"), ("nonsens", "other")):
            self.assertEqual(invite.kind(address), kind, address)

    def test_what_goes_into_an_invite(self):
        local = ["172.20.48.1", "192.168.1.20", "100.101.102.103", "169.254.10.2", "127.0.0.1", "10.0.0.7"]
        # The address traffic leaves by first, two LAN addresses at most, then Tailscale and public.
        self.assertEqual(invite.host_addresses(local, default="192.168.1.20", public=PUBLIC),
                         ["192.168.1.20", "172.20.48.1", "100.101.102.103", PUBLIC])
        self.assertEqual(invite.host_addresses(["192.168.1.20", "192.168.1.20"]), ["192.168.1.20"])
        self.assertEqual(invite.host_addresses(["127.0.0.1"]), [])

    def test_a_joiner_tries_lan_then_tailscale_then_internet(self):
        self.assertEqual(invite.join_order([PUBLIC, "100.101.102.103", "192.168.1.20", "10.0.0.7"]),
                         ["192.168.1.20", "10.0.0.7", "100.101.102.103", PUBLIC])


class ChooseTest(unittest.TestCase):
    """Which of an invite's addresses a joiner keeps."""

    def choose(self, answers, grace=0.5):
        """answers: address -> (delay, outcome)."""
        async def probe(address, port, password, name, timeout):
            delay, outcome = answers[address]
            await asyncio.sleep(delay)
            return outcome, "%s sa %s" % (address, outcome)

        async def run():
            started = time.monotonic()
            found = await flsync.choose(list(answers), 31415, "pw", "Bo", 5.0, grace, probe_fn=probe)
            return found, time.monotonic() - started

        return asyncio.run(run())

    def test_the_lan_is_preferred_when_it_answers_in_time(self):
        (address, _), took = self.choose({PUBLIC: (0.0, "ok"), "192.168.1.20": (0.2, "ok")})
        self.assertEqual(address, "192.168.1.20")
        self.assertLess(took, 0.45)

    def test_an_address_that_never_answers_is_not_waited_for(self):
        (address, _), took = self.choose({"192.168.1.20": (10, "ok"), PUBLIC: (0.0, "ok")})
        self.assertEqual(address, PUBLIC)
        self.assertLess(took, 1.5)

    def test_someone_elses_relay_on_this_lan_is_passed_by(self):
        (address, _), _ = self.choose({"192.168.1.20": (0.0, "password"), "100.101.102.103": (0.1, "ok")})
        self.assertEqual(address, "100.101.102.103")

    def test_when_none_answers_the_most_telling_problem_is_given(self):
        (address, results), _ = self.choose({"192.168.1.20": (0.0, "unreachable"), PUBLIC: (0.05, "password")})
        self.assertIsNone(address)
        self.assertEqual(flsync.join_problem(results, 31415)[0], "password")
        (address, results), _ = self.choose({"192.168.1.20": (0.0, "unreachable"), PUBLIC: (0.0, "unreachable")})
        code, title, text, fix = flsync.join_problem(results, 31415)
        self.assertEqual(code, "unreachable")
        self.assertIn("192.168.1.20 (lokalt nätverk", text)
        self.assertIn(PUBLIC + " (internet", text)
        self.assertTrue(any("31415" in step and "Tailscale" in step for step in fix))


class ProbeTest(unittest.TestCase):
    """How a relay's answer to the joiner's test hello is read."""

    def setUp(self):
        self.loop = LoopThread()
        self.relay = RelayServer(self.loop, password="hemligt")
        self.port = self.relay.start()
        self.servers = []

    def tearDown(self):
        self.relay.stop()
        for server in self.servers:
            self.loop.run(self.close_server(server))
        self.loop.stop()

    @staticmethod
    async def close_server(server):
        server.close()
        await server.wait_closed()

    def probe(self, port, password="hemligt"):
        return self.loop.run(flsync.probe("127.0.0.1", port, password, "Bo", 2.0))

    def answering(self, line):
        """A server that answers any first line with `line`."""
        async def serve(reader, writer):
            await reader.readline()
            writer.write(line)
            await writer.drain()
            writer.close()

        async def start():
            server = await asyncio.start_server(serve, "127.0.0.1", 0)
            self.servers.append(server)
            return server.sockets[0].getsockname()[1]

        return self.loop.run(start())

    def test_answers(self):
        self.assertEqual(self.probe(self.port), ("ok", None))
        self.assertEqual(self.probe(self.port, "fel")[0], "password")
        self.assertEqual(self.probe(free_port())[0], "unreachable")
        newer = self.answering(b'{"t":"error","msg":"relayn k\\u00f6r protokoll 3 men klienten k\\u00f6r protokoll 2"}\n')
        self.assertEqual(self.probe(newer), ("proto", "relayn kör protokoll 3 men klienten kör protokoll 2"))
        self.assertEqual(self.probe(self.answering(b"HTTP/1.1 400 Bad Request\r\n\r\n"))[0], "other")
        # Nobody in the session noticed: a files connection isn't a peer.
        self.assertEqual(self.relay.relay.clients, {})


# --- UPnP -----------------------------------------------------------------------------------

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <friendlyName>Testrouter</friendlyName>
  <deviceList><device>
   <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
   <deviceList><device>
    <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
    <serviceList>
     <service>
      <serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
      <controlURL>/ctl/CmnIfCfg</controlURL>
     </service>
     <service>
      <serviceType>%s</serviceType>
      <serviceId>urn:upnp-org:serviceId:WANIPConn1</serviceId>
      <controlURL>/ctl/IPConn</controlURL>
      <eventSubURL>/evt/IPConn</eventSubURL>
      <SCPDURL>/WANIPCn.xml</SCPDURL>
     </service>
    </serviceList>
   </device></deviceList>
  </device></deviceList>
 </device>
</root>"""

SOAP = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>%s</s:Body></s:Envelope>')


class FakeRouter:
    """An Internet Gateway Device on 127.0.0.1: SSDP on UDP, description and
    SOAP over HTTP. `fail` gives error codes to answer actions with, once each."""

    def __init__(self, external=PUBLIC, service="urn:schemas-upnp-org:service:WANIPConnection:1", location=None):
        self.external = external
        self.service = service
        self.mappings = {}   # port -> {"client", "lease", "description"}
        self.calls = []      # (action, args)
        self.fail = {}
        self.delay = {}      # action -> seconds to take answering it
        self.searches = []
        router = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/rootDesc.xml":
                    self.send_error(404)
                    return
                self.reply(200, DESCRIPTION % router.service)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                action = self.headers["SOAPAction"].strip('"').split("#")[1]
                args = {}
                for element in ET.fromstring(body).iter():
                    if element.tag.rsplit("}", 1)[-1] == action:
                        args = {child.tag: (child.text or "") for child in element}
                self.reply(*router.answer(action, args))

            def reply(self, status, text):
                data = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.location = location or "http://127.0.0.1:%d/rootDesc.xml" % self.http.server_port
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.ssdp = self.udp.getsockname()
        threading.Thread(target=self._answer_searches, daemon=True).start()

    def mapper(self, search_time=1.0):
        return upnp.PortMapper(ssdp=self.ssdp, sources=("127.0.0.1",), search_time=search_time, timeout=3.0)

    def _answer_searches(self):
        while True:
            try:
                data, sender = self.udp.recvfrom(65535)
            except OSError:
                return
            text = data.decode("latin-1")
            target = re.search(r"\r\nST: (.*?)\r\n", text)
            if not text.startswith("M-SEARCH") or not target:
                continue
            self.searches.append(target.group(1))
            answer = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=120\r\nST: %s\r\nUSN: uuid:test::%s\r\n"
                      "LOCATION: %s\r\nSERVER: Test/1.0 UPnP/1.0\r\n\r\n" % (target.group(1), target.group(1),
                                                                             self.location))
            try:
                self.udp.sendto(answer.encode("latin-1"), sender)
            except OSError:
                return

    def answer(self, action, args):
        self.calls.append((action, args))
        time.sleep(self.delay.get(action, 0))
        codes = self.fail.get(action)
        if codes:
            code = codes.pop(0)
            return 500, SOAP % ('<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>'
                                '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>%d'
                                '</errorCode><errorDescription>Test</errorDescription></UPnPError></detail>'
                                '</s:Fault>' % code)
        port = int(args.get("NewExternalPort") or 0)
        values = {}
        if action == "GetExternalIPAddress":
            values = {"NewExternalIPAddress": self.external}
        elif action == "AddPortMapping":
            existing = self.mappings.get(port)
            if existing and existing["client"] != args["NewInternalClient"]:
                self.fail.setdefault(action, []).append(718)
                return self.answer(action, args)
            self.mappings[port] = {"client": args["NewInternalClient"], "lease": int(args["NewLeaseDuration"]),
                                   "description": args["NewPortMappingDescription"]}
        elif action == "DeletePortMapping":
            if port not in self.mappings:
                self.fail.setdefault(action, []).append(714)
                return self.answer(action, args)
            del self.mappings[port]
        elif action == "GetSpecificPortMappingEntry":
            entry = self.mappings.get(port)
            if entry is None:
                self.fail.setdefault(action, []).append(714)
                return self.answer(action, args)
            values = {"NewInternalPort": port, "NewInternalClient": entry["client"], "NewEnabled": 1,
                      "NewPortMappingDescription": entry["description"], "NewLeaseDuration": entry["lease"]}
        else:
            self.fail.setdefault(action, []).append(401)
            return self.answer(action, args)
        return 200, SOAP % ('<u:%sResponse xmlns:u="%s">%s</u:%sResponse>' % (
            action, self.service, "".join("<%s>%s</%s>" % (k, v, k) for k, v in values.items()), action))

    def actions(self):
        return [action for action, _ in self.calls]

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.udp.close()


class UpnpTest(unittest.TestCase):
    def setUp(self):
        self.router = FakeRouter()

    def tearDown(self):
        self.router.close()

    def test_a_port_is_opened_and_closed_again(self):
        mapper = self.router.mapper()
        mapping = mapper.open(40001)
        self.assertEqual((mapping.external_ip, mapping.internal_ip, mapping.lease), (PUBLIC, "127.0.0.1", upnp.LEASE))
        self.assertEqual(self.router.mappings, {40001: {"client": "127.0.0.1", "lease": upnp.LEASE,
                                                        "description": "FLSync"}})
        self.assertIn("urn:schemas-upnp-org:device:InternetGatewayDevice:1", self.router.searches)
        mapper.renew(mapping)
        self.assertEqual(self.router.actions().count("AddPortMapping"), 2)
        mapper.close(mapping)
        self.assertEqual(self.router.mappings, {})
        mapper.close(mapping)  # already gone: nothing to complain about

    def test_a_router_that_only_keeps_mappings_for_good(self):
        self.router.fail["AddPortMapping"] = [upnp.PERMANENT_ONLY]
        mapping = self.router.mapper().open(40001)
        self.assertEqual((mapping.lease, self.router.mappings[40001]["lease"]), (0, 0))

    def test_a_port_another_computer_has(self):
        self.router.mappings[40001] = {"client": "192.168.1.33", "lease": 0, "description": "Spel"}
        with self.assertRaises(upnp.UpnpError) as caught:
            self.router.mapper().open(40001)
        self.assertEqual(caught.exception.code, upnp.CONFLICT)
        self.assertIn("192.168.1.33", str(caught.exception))
        self.assertEqual(self.router.mappings[40001]["client"], "192.168.1.33")

    def test_our_own_leftover_mapping_is_replaced(self):
        self.router.mappings[40001] = {"client": "127.0.0.1", "lease": 0, "description": "FLSync"}
        self.router.fail["AddPortMapping"] = [upnp.CONFLICT]  # some routers say so even for the same computer
        mapping = self.router.mapper().open(40001)
        self.assertEqual(mapping.external_ip, PUBLIC)
        self.assertEqual(self.router.mappings[40001]["lease"], upnp.LEASE)

    def test_a_locked_router(self):
        self.router.fail["AddPortMapping"] = [upnp.NOT_AUTHORIZED]
        with self.assertRaises(upnp.UpnpError) as caught:
            self.router.mapper().open(40001)
        self.assertIn("låst", str(caught.exception))

    def test_a_ppp_connection(self):
        self.router.service = "urn:schemas-upnp-org:service:WANPPPConnection:1"
        mapping = self.router.mapper().open(40001)
        self.assertEqual(mapping.gateway.service, self.router.service)
        self.assertIn(40001, self.router.mappings)

    def test_no_router_answers(self):
        silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        silent.bind(("127.0.0.1", 0))
        try:
            started = time.monotonic()
            with self.assertRaises(upnp.UpnpError) as caught:
                upnp.PortMapper(ssdp=silent.getsockname(), sources=("127.0.0.1",), search_time=0.5).open(40001)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            silent.close()
        self.assertIn("ingen router svarade", str(caught.exception))

    def test_only_addresses_on_this_network_are_asked(self):
        for url, ok in (("http://192.168.1.1:5000/rootDesc.xml", True), ("http://127.0.0.1/x", True),
                        ("http://81.2.3.4/rootDesc.xml", False), ("https://192.168.1.1/x", False),
                        ("http://router.example/x", False), ("file:///c:/x", False)):
            self.assertEqual(upnp.local_url(url), ok, url)
        far = FakeRouter(location="http://81.2.3.4:5000/rootDesc.xml")
        try:
            with self.assertRaises(upnp.UpnpError):
                far.mapper(search_time=0.3).open(40001)
            self.assertEqual(far.calls, [])
        finally:
            far.close()


# --- settings and the checklist --------------------------------------------------------------

class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="flsync-settings-")
        self.path = os.path.join(self.tmp, "FLSync", "settings.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kept_between_runs(self):
        settings = flsync.Settings(self.path).load()
        self.assertEqual(settings.data, {})
        settings.set(name="Anna", mode="join", last_invite="FLS1-AAAA")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"name": "Anna", "mode": "join", "last_invite": "FLS1-AAAA"})
        again = flsync.Settings(self.path).load()
        self.assertEqual((again.get("name"), again.get("mode"), again.get("missing", 5)), ("Anna", "join", 5))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path))], ["settings.json"])

    def test_a_broken_file_gives_the_defaults(self):
        os.makedirs(os.path.dirname(self.path))
        for text in ("{trasig", "[1, 2]", ""):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
            said = []
            self.assertEqual(flsync.Settings(self.path, said.append).load().data, {})
        flsync.Settings(self.path).load().set(name="Bo")
        self.assertEqual(flsync.Settings(self.path).load().get("name"), "Bo")

    def test_names(self):
        self.assertEqual(flsync.clean_name("  Anna\x00\n "), "Anna")
        self.assertEqual(len(flsync.clean_name("x" * 100)), 40)
        self.assertEqual(flsync.clean_name(None), "")


class DiagnoseTest(unittest.TestCase):
    GOOD = {"loopmidi": True, "ports": (["FLSync"], ["FLSync"]), "midi_open": True, "fl": True, "fl_heard": True,
            "fl_version": FL25}
    SETTINGS = {"fl": "FL Studio 25",
                "input": {"Type": "-3", "ScriptFolder": "FLSync", "Port": "10", "Enabled": "1"},
                "output": {"Port": "10"}, "outputs": {"FLSync": {"Port": "10"}, "Synth": {"Port": "-1"}}}

    def states(self, **facts):
        return {c["id"]: c for c in diagnose.checks(dict(self.GOOD, **facts))}

    def test_all_good(self):
        found = self.states()
        self.assertEqual({k: c["state"] for k, c in found.items()}, {"loopmidi": "ok", "port": "ok", "fl": "ok"})
        self.assertIn(FL25, found["fl"]["title"])

    def test_loopmidi_not_running(self):
        found = self.states(loopmidi=False, midi_open=False, fl_heard=False)
        self.assertEqual((found["loopmidi"]["state"], found["port"]["state"]), ("bad", "wait"))
        self.assertIn("Starta loopMIDI", found["loopmidi"]["fix"][0])

    def test_no_port_named_flsync(self):
        found = self.states(ports=(["Keystation"], ["Keystation"]), midi_open=False, fl_heard=False)
        self.assertEqual(found["port"]["state"], "bad")
        self.assertIn("loopMIDI", found["port"]["fix"][0])
        self.assertIn("Starta sedan om FL Studio", found["port"]["fix"][0])

    def test_the_port_is_taken(self):
        found = self.states(midi_open=False, fl_heard=False,
                            midi_problem="MIDI-ingången 'FLSync' används redan av ett annat program")
        self.assertEqual(found["port"]["state"], "bad")
        self.assertIn("används redan", found["port"]["detail"])

    def test_fl_not_running_or_starting(self):
        self.assertEqual(self.states(fl=False, fl_heard=False)["fl"]["state"], "wait")
        found = self.states(fl_heard=False, fl_age=20, silent_for=20)
        self.assertEqual((found["fl"]["state"], found["fl"]["title"]), ("wait", "FL Studio startar ..."))
        self.assertEqual(self.states(fl_heard=False, fl_age=300, silent_for=3)["fl"]["state"], "wait")

    def test_fl_running_but_not_heard(self):
        settings = json.loads(json.dumps(self.SETTINGS))
        settings["input"].update(Enabled="0", ScriptFolder="")
        settings["output"]["Port"] = "11"
        settings["outputs"]["Synth"]["Port"] = "11"
        found = self.states(fl_heard=False, fl_age=600, silent_for=30, fl_started=1000.0, loopmidi_started=2000.0,
                            settings=settings)["fl"]
        self.assertEqual(found["state"], "bad")
        fix = "\n".join(found["fix"])
        self.assertIn("FL startades innan loopMIDI", found["fix"][0])  # the likeliest cause first
        for words in ("inte aktiverad", "Controller type FLSync", "Port-nummer 11 men ingången 10",
                      "Synth har också Port-nummer 11", "Options > MIDI settings"):
            self.assertIn(words, fix)

    def test_fls_settings(self):
        self.assertEqual(diagnose.settings_problems(self.SETTINGS), [])
        self.assertEqual(diagnose.settings_problems(None), [])
        unseen = dict(self.SETTINGS, input=None, output=None)
        self.assertIn("har inte sett", diagnose.settings_problems(unseen)[0])
        no_output = dict(self.SETTINGS, output={"Port": "-1"})
        self.assertIn("Utgången FLSync har inget Port-nummer", diagnose.settings_problems(no_output)[0])

    def test_session_items_come_from_the_launcher(self):
        found = self.states(relay={"state": "bad", "title": "Värdens FLSync svarar inte", "fix": "gör så här"},
                            session={"state": "wait", "title": "Ansluter"})
        self.assertEqual((found["relay"]["state"], found["relay"]["fix"]), ("bad", ["gör så här"]))
        self.assertEqual(found["session"]["state"], "wait")


# --- the page --------------------------------------------------------------------------------

class PageTest(LauncherTest):
    def test_only_the_launchers_own_page_gets_answers(self):
        anna = self.launcher("Anna")
        port = anna.port
        self.assertEqual(anna.request("GET", "/api/status")[0], 200)
        self.assertEqual(anna.request("GET", "/api/status", token=False)[0], 403)
        self.assertEqual(anna.request("GET", "/api/status", headers={webui.TOKEN_HEADER: "fel"})[0], 403)
        self.assertEqual(anna.request("GET", "/api/status", headers={webui.TOKEN_HEADER: "nyckel-åäö"})[0], 403)
        # Another name for 127.0.0.1 (DNS rebinding) or another site's page: refused, token or not.
        self.assertEqual(anna.request("GET", "/api/status", headers={"Host": "evil.example:%d" % port})[0], 403)
        self.assertEqual(anna.request("GET", "/", headers={"Host": "evil.example:%d" % port})[0], 403)
        self.assertEqual(anna.request("GET", "/api/status", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(anna.request("GET", "/api/status", headers={"Origin": "null"})[0], 403)
        self.assertEqual(anna.request("POST", "/api/host", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(anna.request("POST", "/api/host", {}, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertIsNone(anna.app.mode)
        # What our own page sends.
        own = {"Origin": "http://127.0.0.1:%d" % port, "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(anna.request("GET", "/api/status", headers=own)[0], 200)
        self.assertEqual(anna.request("GET", "/api/status", headers={"Host": "localhost:%d" % port})[0], 200)
        self.assertEqual(anna.request("GET", "/api/ping")[1], {"ok": True, "app": "FLSync", "version": flsync.VERSION})
        self.assertEqual(anna.request("GET", "/api/nothing")[0], 404)
        self.assertEqual(anna.request("PUT", "/api/status")[0], 405)
        self.assertEqual(anna.request("POST", "/api/leave", headers={"Content-Type": "text/plain"})[0], 400)

    def test_the_page_is_self_contained(self):
        anna = self.launcher("Anna")
        status, page = anna.request("GET", "/?t=" + anna.app.token, token=False)
        self.assertEqual(status, 200)
        headers = anna.last_headers
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        nonce = re.search(r"script-src 'nonce-([^']+)'", headers["Content-Security-Policy"]).group(1)
        self.assertIn('<script nonce="%s">' % nonce, page)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual((headers["X-Frame-Options"], headers["Referrer-Policy"]), ("DENY", "no-referrer"))
        self.assertNotRegex(page, r"https?://|\b(src|href)\s*=|url\(|@import")  # no CDN, fonts or links out
        self.assertIn("Starta session", page)
        self.assertIn("Gå med", page)
        second = anna.request("GET", "/", token=False)
        self.assertNotIn(nonce, second[1])  # a new nonce every time
        connection = http.client.HTTPConnection("127.0.0.1", anna.port, timeout=5)
        try:
            connection.request("GET", "/favicon.ico")  # what the browser asks for by itself, without the token
            icon = connection.getresponse()
            self.assertEqual((icon.status, icon.getheader("Content-Type")), (200, "image/x-icon"))
            self.assertEqual(icon.read()[:4], b"\x00\x00\x01\x00")
        finally:
            connection.close()
        self.assertEqual(anna.request("GET", "/favicon.ico", headers={"Host": "evil.example"})[0], 403)

    def test_broken_requests(self):
        anna = self.launcher("Anna")
        for raw in (b"NONSENS\r\n\r\n", b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 999999\r\n\r\n"
                    % anna.port, b"GET / HTTP/1.1\r\ntrasig rubrik\r\n\r\n"):
            with socket.create_connection(("127.0.0.1", anna.port), timeout=5) as sock:
                sock.sendall(raw)
                answer = sock.makefile("rb").read()
            self.assertRegex(answer.split(b"\r\n")[0], rb"HTTP/1.1 (400|413)")
        self.assertEqual(anna.request("GET", "/api/status")[0], 200)  # still there

    def test_the_checklist(self):
        anna = self.launcher("Anna")
        anna.system.procs = [winsys.Process("fl64 (scaled).exe", 100, time.time() - 600)]  # counts as FL
        anna.system.ports = (["Keystation"], ["Keystation"])
        self.wait_for(lambda: anna.app.machine.get("processes") == anna.system.procs, why="tittade aldrig")
        checks = {c["id"]: c for c in anna.status()["checks"]}
        self.assertEqual(checks["loopmidi"]["state"], "bad")
        self.assertEqual(checks["session"]["state"], "off")
        anna.system.procs.append(winsys.Process("loopmidi.exe", 101, time.time() - 900))
        anna.system.ports = (["FLSync"], ["FLSync"])
        hello(anna.fl, FL25)  # FL is heard (and stays out: no session yet)
        self.wait_for(lambda: anna.app.bridge.fl_last_heard is not None)
        self.settle(0.5)
        checks = {c["id"]: c for c in anna.status()["checks"]}
        self.assertEqual([checks[k]["state"] for k in ("loopmidi", "port", "fl")], ["ok", "ok", "ok"])
        self.assertIn(FL25, checks["fl"]["title"])


# --- launchers in a session -----------------------------------------------------------------------

class SessionTest(LauncherTest):
    def start(self, project=None):
        self.anna = self.launcher("Anna", project=project)
        self.bo = self.launcher("Bo")
        self.code = self.host(self.anna)
        self.join(self.bo, self.code)
        self.wait_for(lambda: len(self.anna.app.relay.clients) == 2)
        return self.anna, self.bo

    def test_before_a_session_is_chosen_fl_stays_out(self):
        anna = self.launcher("Anna")
        hello(anna.fl, FL25)
        self.wait_for(lambda: anna.fl.got("link"), why="FL fick inget besked")
        self.assertIn("ingen session vald", anna.fl.got("link")[0]["msg"])
        hello(anna.fl, FL25)  # FL asks again a few seconds later: no more noise
        self.settle()
        self.assertEqual(len(anna.fl.got("link")), 1)
        self.assertFalse(anna.app.bridge.in_session())
        status = anna.status()
        self.assertIsNone(status["mode"])
        self.assertEqual(status["summary"]["text"], "Ingen session")
        self.assertEqual(status["settings"]["relay_port"], 31415)

    def test_hosting_and_joining(self):
        anna, bo = self.start()
        status = anna.status()
        self.assertEqual(status["mode"], "host")
        self.assertEqual(status["invite"]["code"], self.code)
        self.assertEqual([a["ip"] for a in status["invite"]["addresses"]], ["127.0.0.1"])
        self.assertEqual(status["router"]["state"], "off")  # no UPnP in this test
        self.assertEqual(sorted((p["name"], p["flv"], p["me"]) for p in status["people"]),
                         [("Anna", FL25, True), ("Bo", FL26, False)])
        self.assertEqual(status["summary"], {"text": "Du är värd · live med Bo", "state": "ok"})
        checks = {c["id"]: c["state"] for c in status["checks"]}
        self.assertEqual((checks["relay"], checks["session"]), ("ok", "ok"))
        self.assertTrue(any("Bo anslöt" in line["text"] for line in status["log"]))  # the relay's log

        status = bo.status()
        self.assertEqual(status["mode"], "join")
        self.assertIn("127.0.0.1:%d" % anna.app.relay_port, status["joined_text"])
        self.assertEqual([(p["name"], p["me"]) for p in status["people"]], [("Bo", True), ("Anna", False)])
        self.assertIn("äldre FL", status["people_note"])  # Anna can't open what Bo saves
        self.assertEqual(status["summary"], {"text": "Live med Anna", "state": "ok"})
        self.assertIn("nyare FL", anna.status()["people_note"])

        with open(os.path.join(bo.folder, "data", "settings.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual((saved["mode"], saved["last_invite"], saved["name"]), ("join", self.code, "Bo"))
        with open(os.path.join(anna.folder, "data", "settings.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual((saved["mode"], saved["relay_port"]), ("host", anna.app.relay_port))
        self.assertEqual(invite.decode(self.code).password, saved["host_password"])

    def test_versions_and_opening_them(self):
        project = os.path.join(self.tmp, "Anna", "Beat.flp")
        anna, bo = self.start(project=project)
        save(project, make_project(title="Beat"))
        self.wait_for(lambda: bo.app.bridge.received, 8, "versionen kom aldrig fram")
        version = bo.status()["version"]
        self.assertEqual((version["v"], version["name"], version["by"], version["can_open"]), (1, "Beat", "Anna", True))
        self.assertTrue(version["state_text"].startswith("Hämtad till"))
        self.assertEqual(version["details"], "0 samples, %.1f MB" % (len(make_project(title="Beat")) / 1e6))
        mine = anna.status()["version"]
        self.assertEqual((mine["can_open"], mine["state_text"]), (False, "Det är din version. Den har du redan öppen."))
        self.assertEqual(anna.post("open")[1]["error"]["title"], "Det här är din egen version")

        # Bo opens it. First FL hands the file to the FL that runs (a second FL64.exe for a moment): fine.
        bo.app.open_poll, bo.app.second_fl_stays = 0.05, 0.6
        bo.system.procs = [winsys.Process("fl64.exe", 100, time.time() - 300)]

        def hand_over(path):
            stub = winsys.Process("fl64.exe", 150, time.time())
            bo.system.procs.append(stub)
            threading.Timer(0.2, bo.system.procs.remove, [stub]).start()

        bo.system.on_open = hand_over
        status, answer = bo.post("open")
        self.assertEqual((status, answer), (200, {"ok": True}))
        path = bo.status()["version"]["path"]
        self.assertEqual(bo.system.opened, [path])
        self.assertTrue(os.path.isfile(path))
        self.settle(1.2)
        self.assertIsNone(bo.status()["open_warning"])
        # Then FL starts a second time instead of using the one that runs.
        bo.system.on_open = lambda path: bo.system.procs.append(
            winsys.Process("fl64 (scaled).exe", 200, time.time()))
        self.assertEqual(bo.post("open"), (200, {"ok": True}))
        self.wait_for(lambda: bo.app.open_warning, why="ingen varning om det nya fönstret")
        warning = bo.status()["open_warning"]
        self.assertIn("nytt fönster", warning["title"])
        self.assertIn("File > Open", warning["text"])
        self.assertIn(path, warning["text"])
        bo.post("open", folder=True)
        self.assertEqual(bo.system.opened, [path, path, os.path.dirname(path)])

        # What the bridge says about plugins missing here and a newer FL shows on the page.
        newest = bo.call(lambda: dict(bo.app.bridge.newest))
        notice = {"t": "snapshot", "v": newest["v"], "name": "Beat", "fp": newest["fp"], "by": "Anna",
                  "from": newest["from"], "path": path, "flv": "26.1.7.5512",
                  "missing": [{"plugin": "Serum", "channels": [3, 7]}, {"plugin": "Fruity X", "mixer": True}, "Y"]}
        bo.call(bo.app.bridge.to_fl, notice)
        version = bo.status()["version"]
        self.assertEqual(version["missing"], ["Serum (kanal 3, 7)", "Fruity X (mixern)", "Y"])
        self.assertIsNone(version["newer"])  # Bo has FL 26.1.7 himself
        notice["flv"] = "27.0.0.1"
        bo.call(bo.app.bridge.to_fl, notice)
        self.assertIn("Sparad i en nyare FL (27.0.0.1) än din (%s)" % FL26, bo.status()["version"]["newer"])
        bo.call(bo.app.bridge.to_fl, {"t": "loaded", "v": newest["v"], "fp": newest["fp"]})
        self.assertEqual(bo.status()["version"]["state_text"], "Du har den öppen i FL.")

    def test_nothing_to_open_yet(self):
        anna, bo = self.start()
        status, answer = bo.post("open")
        self.assertEqual((status, answer["error"]["title"]), (400, "Ingen version har delats än"))
        self.assertEqual(bo.system.opened, [])

    def test_notes_and_the_pen(self):
        anna, bo = self.start()
        cia = self.raw(anna.app.relay_port, "Cia", invite.decode(self.code).password, fp=FP_A, flv=FL25)
        self.wait_for(lambda: len(bo.app.peers) == 2)
        cia.send({"t": "notes_put", "p": 1, "c": 4, "ppq": 96, "notes": [NOTE], "fp": FP_A})
        self.wait_for(lambda: anna.app.notes_seen and bo.app.notes_seen)
        for launcher in (anna, bo):
            notes = launcher.status()["notes"]
            self.assertEqual(notes[0]["text"], "Cia skickade 1 noter till kanal 5 i mönster 1")

        cia.send({"t": "fp", "fp": FP_B})  # Cia adds a channel
        self.wait_for(lambda: bo.app.pen is not None)
        for launcher in (anna, bo):
            status = launcher.status()
            self.assertEqual(status["pen"]["text"], "Cia ändrar projektets struktur. När Cia har sparat: öppna den "
                                                    "nya versionen.")
            self.assertEqual([p["name"] for p in status["people"] if p["pen"]], ["Cia"])
        cia.close()
        self.clients.remove(cia)
        self.wait_for(lambda: bo.app.pen is None and len(bo.app.peers) == 1)
        self.assertIsNone(anna.status()["pen"])

    def test_leaving(self):
        anna, bo = self.start()
        self.assertEqual(bo.post("leave"), (200, {"ok": True}))
        status = bo.status()
        self.assertEqual((status["mode"], status["people"]), (None, []))
        self.wait_for(lambda: any("lämnade" in m["msg"] for m in bo.fl.got("link")), why="Bos FL fick inget besked")
        self.wait_for(lambda: len(anna.app.relay.clients) == 1)
        hello(bo.fl, FL26)  # FL asks to join again: it stays out
        self.settle()
        self.assertFalse(bo.app.bridge.in_session())

        port = anna.app.relay_port
        self.assertEqual(anna.post("leave"), (200, {"ok": True}))
        self.assertIsNone(anna.status()["invite"])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_the_guest_sees_when_the_host_is_gone(self):
        anna, bo = self.start()
        anna.post("leave")
        self.wait_for(lambda: bo.app.link_problem, why="Bo märkte inget")
        status = bo.status()
        self.assertEqual(status["summary"], {"text": "Värden svarar inte", "state": "bad"})
        relay = [c for c in status["checks"] if c["id"] == "relay"][0]
        self.assertEqual(relay["state"], "bad")
        self.assertEqual(status["people"], [])
        self.assertEqual(status["mode"], "join")  # it keeps trying, for when the host is back

    def test_a_new_code_keeps_those_in_but_not_old_invites(self):
        anna, bo = self.start()
        self.assertEqual(anna.post("newcode"), (200, {"ok": True}))
        new = anna.status()["invite"]["code"]
        self.assertNotEqual(new, self.code)
        self.assertEqual(invite.decode(new).addresses, invite.decode(self.code).addresses)
        self.settle()
        self.assertTrue(bo.app.bridge.in_session())
        # Bo's connection drops: joining again with the old password is refused, so he leaves and is told why.
        bo.call(bo.app.bridge.close_session)
        hello(bo.fl, FL26)
        self.wait_for(lambda: bo.app.mode is None, why="Bo lämnade inte")
        error = bo.status()["error"]
        self.assertEqual((error["code"], error["title"]), ("password", "Fel lösenord för sessionen"))
        status, answer = bo.post("join", code=self.code)
        self.assertEqual((status, answer["error"]["code"]), (400, "password"))
        self.join(bo, new)

    def test_changing_name(self):
        anna, bo = self.start()
        self.assertEqual(bo.post("name", name="  Bosse "), (200, {"ok": True}))
        self.assertEqual(bo.status()["name"], "Bosse")
        with open(os.path.join(bo.folder, "data", "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["name"], "Bosse")
        self.wait_for(lambda: any("byter namn" in m["msg"] for m in bo.fl.got("link")))
        hello(bo.fl, FL26)  # FL joins again
        self.wait_for(lambda: "Bosse" in [c.name for c in anna.app.relay.clients.values()])
        bo.post("name", name="")  # nothing: the Windows name
        self.assertEqual(bo.app.name, "Testare")

    def test_one_session_at_a_time(self):
        anna, bo = self.start()
        for launcher, action, body in ((anna, "host", {}), (anna, "join", {"code": self.code}),
                                       (bo, "host", {}), (bo, "join", {"code": self.code})):
            status, answer = launcher.post(action, **body)
            self.assertEqual((status, answer["error"]["title"]), (400, "Du är redan med i en session"))
        self.assertTrue(anna.app.bridge.in_session() and bo.app.bridge.in_session())


class RealScriptTest(LauncherTest):
    """The real FLSync script, in a simulated FL, behind two launchers."""

    def test_fl_waits_until_a_session_is_chosen_then_syncs(self):
        anna = self.launcher("Anna", fl=FakeFL("Anna"))
        bo = self.launcher("Bo", fl=FakeFL("Bo"))
        self.wait_for(lambda: any("ingen session vald" in line for line in anna.fl.logs), 5,
                      "FL hörde inte att ingen session är vald")
        self.assertFalse(anna.fl.client.joined)
        self.assertEqual(anna.post("host", port=free_port()), (200, {"ok": True}))
        self.wait_for(lambda: anna.app.invite and anna.fl.client.joined, 10, "Annas FL kom aldrig med")
        self.assertEqual(bo.post("join", code=anna.app.invite), (200, {"ok": True}))
        self.wait_for(lambda: bo.fl.client.joined and not bo.fl.client.pending, 10, "Bos FL kom aldrig med")

        anna.fl.set_volume(2, 0.25)
        self.wait_for(lambda: abs(bo.fl.volume(2) - 0.25) < 0.01, 5, "ändringen kom inte fram")
        people = anna.status()["people"]
        self.assertEqual(sorted(p["name"] for p in people), ["Anna", "Bo"])
        self.assertTrue(all(p["flv"] for p in people), people)  # FL says its version in its hello

        self.assertEqual(bo.post("leave"), (200, {"ok": True}))
        self.wait_for(lambda: not bo.fl.client.joined and len(anna.app.relay.clients) == 1, 5)
        self.settle(1.5)  # FL asks to join again: it stays out
        self.assertFalse(bo.fl.client.joined)


class JoinErrorTest(LauncherTest):
    def setUp(self):
        super().setUp()
        self.bo = self.launcher("Bo")

    def assertJoinFails(self, code, kind, words):
        status, answer = self.bo.post("join", code=code)
        self.assertEqual((status, answer["error"]["code"]), (400, kind), answer)
        self.assertIn(words, json.dumps(answer["error"], ensure_ascii=False))
        page = self.bo.status()
        self.assertEqual((page["mode"], page["error"]["code"]), (None, kind))
        return answer["error"]

    def test_a_broken_code(self):
        good = invite.encode(["127.0.0.1"], free_port(), PASSWORD)
        self.assertJoinFails(good[:-2] + ("A" if good[-2] != "A" else "B") + good[-1], "code", "stämmer inte")
        self.assertJoinFails("hej", "code", "ser inte ut som")

    def test_nobody_answers(self):
        port = free_port()
        error = self.assertJoinFails(invite.encode(["127.0.0.1"], port, PASSWORD), "unreachable", "127.0.0.1")
        self.assertIn("Tailscale", " ".join(error["fix"]))

    def test_the_wrong_password(self):
        relay = RelayServer(self.loop, password="hemligt")
        self.relays.append(relay)
        port = relay.start()
        self.assertJoinFails(invite.encode(["127.0.0.1"], port, PASSWORD), "password", "ny kod")

    def test_another_version_of_flsync(self):
        async def serve(reader, writer):
            await reader.readline()
            writer.write(b'{"t":"error","msg":"relayn k\\u00f6r protokoll 3 men klienten k\\u00f6r protokoll 2. '
                         b'Uppdatera s\\u00e5 att de matchar."}\n')
            await writer.drain()
            writer.close()

        async def start():
            return await asyncio.start_server(serve, "127.0.0.1", 0)

        server = self.loop.run(start())
        try:
            port = server.sockets[0].getsockname()[1]
            self.assertJoinFails(invite.encode(["127.0.0.1"], port, PASSWORD), "proto", "protokoll 3")
        finally:
            async def close():
                server.close()
                await server.wait_closed()

            self.loop.run(close())

    def test_a_port_that_is_taken(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            port = other.getsockname()[1]
            status, answer = self.bo.post("host", port=port)
        self.assertEqual((status, answer["error"]["code"]), (400, "port"))
        self.assertIn("används redan", answer["error"]["title"])
        self.assertIsNone(self.bo.app.mode)
        status, answer = self.bo.post("host", port=70000)
        self.assertEqual(answer["error"]["title"], "Ogiltig port")


class RouterSessionTest(LauncherTest):
    """Hosting with UPnP: the fake router opens the port and says the public address."""

    def setUp(self):
        super().setUp()
        self.router = FakeRouter()
        self.system = FakeSystem()
        self.system.addresses = ["192.168.1.20", "100.101.102.103", "127.0.0.1"]
        self.system.default = "192.168.1.20"
        self.system.public = "81.9.9.9"

    def tearDown(self):
        super().tearDown()
        self.router.close()

    def test_the_router_opens_the_port_and_closes_it_again(self):
        anna = self.launcher("Anna", system=self.system, mapper=self.router.mapper(), ip_lookup=True)
        code = self.host(anna)
        found = invite.decode(code)
        self.assertEqual(found.addresses, ["192.168.1.20", "100.101.102.103", PUBLIC])
        port = anna.app.relay_port
        self.assertEqual(self.router.mappings[port]["client"], "127.0.0.1")
        router = anna.status()["router"]
        self.assertEqual(router["state"], "ok")
        self.assertIn(PUBLIC, router["title"])
        self.assertEqual(self.system.lookups, 0)  # the router said it: nobody else is asked
        anna.post("leave")
        self.assertEqual(self.router.mappings, {})
        self.assertEqual(self.router.actions()[-1], "DeletePortMapping")

    def test_leaving_while_the_router_opens_the_port(self):
        self.router.delay["AddPortMapping"] = 1.0
        anna = self.launcher("Anna", system=self.system, mapper=self.router.mapper())
        self.assertEqual(anna.post("host", port=free_port())[0], 200)
        self.wait_for(lambda: "AddPortMapping" in self.router.actions(), why="routern tillfrågades aldrig")
        self.assertEqual(anna.post("leave"), (200, {"ok": True}))  # the answer is still on its way
        self.assertEqual(self.router.mappings, {})
        self.assertEqual(self.router.actions()[-1], "DeletePortMapping")
        self.assertIsNone(anna.status()["invite"])

    def test_a_router_without_a_public_address(self):
        self.router.external = "100.70.1.2"  # the provider's shared address space (CGNAT)
        anna = self.launcher("Anna", system=self.system, mapper=self.router.mapper(), ip_lookup=True)
        code = self.host(anna)
        self.assertEqual(invite.decode(code).addresses, ["192.168.1.20", "100.101.102.103"])
        router = anna.status()["router"]
        self.assertEqual(router["state"], "bad")
        self.assertIn("CGNAT", router["detail"])
        self.assertIn("Tailscale", router["fix"][0])
        self.assertEqual(self.system.lookups, 0)

    def test_without_upnp_the_session_still_starts(self):
        self.router.fail["AddPortMapping"] = [upnp.NOT_AUTHORIZED]
        anna = self.launcher("Anna", system=self.system, mapper=self.router.mapper(), ip_lookup=True)
        code = self.host(anna)
        self.assertEqual(invite.decode(code).addresses, ["192.168.1.20", "100.101.102.103", "81.9.9.9"])
        router = anna.status()["router"]
        self.assertEqual(router["state"], "bad")
        self.assertIn("låst", router["detail"])
        self.assertIn("öppna port %d (TCP) i routern mot 192.168.1.20" % anna.app.relay_port, router["fix"][1])
        self.assertTrue(anna.app.bridge.in_session())


class SettingsAppTest(LauncherTest):
    def test_the_hosts_code_stays_the_same_between_runs(self):
        folder = os.path.join(self.tmp, "Anna")
        first = self.launcher("Anna")
        port = free_port()
        code = self.host(first, port=port)
        first.stop()
        self.launchers.remove(first)
        again = Launcher(self.loop, folder, "Anna")
        self.launchers.append(again)
        self.assertEqual(self.host(again, port=port), code)
        status = again.status()
        self.assertEqual((status["settings"]["last_mode"], status["settings"]["relay_port"]), ("host", port))

    def test_the_name(self):
        anna = self.launcher("Anna")
        self.assertEqual(anna.status()["name"], "Anna")
        unnamed = Launcher(self.loop, os.path.join(self.tmp, "x"), None)
        self.launchers.append(unnamed)
        self.assertEqual(unnamed.app.name, "Testare")  # the Windows user name


class RunTest(LauncherTest):
    def test_run_opens_the_page_and_quits_from_it(self):
        anna = self.launcher("Anna", start=False)
        data_dir = anna.app.data_dir
        self.assertIsNone(flsync.find_running(data_dir))
        running = asyncio.run_coroutine_threadsafe(anna.app.run(open_browser=True), self.loop.loop)
        self.wait_for(lambda: anna.system.browsed, why="webbläsaren öppnades inte")
        url = anna.system.browsed[0]
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/\?t=[\w-]+$")
        self.assertEqual(flsync.find_running(data_dir), url)  # what a second double-click opens
        self.assertEqual(anna.post("quit"), (200, {"ok": True}))
        running.result(15)
        self.assertIsNone(flsync.find_running(data_dir))
        self.assertFalse(os.path.exists(flsync.instance_path(data_dir)))

    def test_options(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            flsync.main(["--help"])
        for word in ("--no-browser", "--host", "--join", "--name", "--relay-port", "--no-upnp"):
            self.assertIn(word, out.getvalue())


class ProgramTest(unittest.TestCase):
    """app/flsync.py started as a program, the way FLSync.cmd starts it (with
    this interpreter): a MIDI port that doesn't exist, no browser, no router,
    a temporary data folder and free ports."""

    def test_start_host_and_quit(self):
        data = tempfile.mkdtemp(prefix="flsync-program-")
        relay_port = free_port()
        program = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "app", "flsync.py"), "--no-browser", "--no-upnp", "--no-ip-lookup",
             "--data-dir", data, "--projects-dir", os.path.join(data, "Projects"), "--bind", "127.0.0.1",
             "--relay-port", str(relay_port), "--midi-port", "FLSync test %d" % relay_port, "--name", "Prov",
             "--host"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        output = []
        try:
            url = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and url is None:
                line = program.stdout.readline()
                if not line:
                    break
                output.append(line)
                found = re.search(r"(http://127\.0\.0\.1:\d+/\?t=[\w-]+)", line)
                url = found and found.group(1)
            self.assertTrue(url, "".join(output))
            port, token = re.search(r":(\d+)/\?t=(.+)$", url).groups()
            self.assertEqual(flsync.find_running(data), url)

            def call(method, path, body=None):
                connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=10)
                headers = {webui.TOKEN_HEADER: token}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                try:
                    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
                    response = connection.getresponse()
                    raw = response.read()
                finally:
                    connection.close()
                self.assertEqual(response.status, 200, raw)
                return json.loads(raw)

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not call("GET", "/api/status")["invite"]:
                time.sleep(0.1)
            status = call("GET", "/api/status")
            self.assertEqual((status["mode"], status["name"]), ("host", "Prov"))
            self.assertEqual(invite.decode(status["invite"]["code"]).port, relay_port)
            self.assertEqual(call("POST", "/api/quit", {}), {"ok": True})
            program.wait(20)
            output.append(program.stdout.read())
        finally:
            if program.poll() is None:
                program.kill()
                program.wait(5)
            program.stdout.close()
            shutil.rmtree(data, ignore_errors=True)
        text = "".join(output)
        self.assertEqual(program.returncode, 0, text)
        self.assertIn("hittar ingen MIDI", text)  # it looked for its (nonexistent) port and kept trying
        self.assertIn("Sessionen är avslutad", text)


if __name__ == "__main__":
    unittest.main()
