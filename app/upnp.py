"""Opening the relay's port in the router with UPnP (an Internet Gateway
Device), stdlib only.

    mapper = PortMapper()
    mapping = mapper.open(31415)   # UpnpError, with a Swedish message, if it can't
    mapping.external_ip            # the router's own public address, if it says
    mapper.close(mapping)

Discovery is SSDP: an M-SEARCH over UDP multicast, which routers answer with
the URL of their description XML. That names the control URL of the WAN
connection service (WANIPConnection or WANPPPConnection), and the rest is
SOAP over HTTP to it. Everything here blocks: the launcher runs it on a
worker thread.
"""

import ipaddress
import select
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

SSDP = ("239.255.255.250", 1900)
TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:", "urn:schemas-upnp-org:service:WANPPPConnection:")
MAX_XML = 256 << 10
LEASE = 7200            # seconds; the launcher renews the mapping well before it ends
DESCRIPTION = "FLSync"

# Routers' error codes (UPnP IGD).
NOT_AUTHORIZED = 606
NO_SUCH_ENTRY = 714
CONFLICT = 718
PERMANENT_ONLY = 725


class UpnpError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class Gateway:
    """One router's WAN connection service."""

    def __init__(self, control_url, service, local_ip):
        self.control_url = control_url
        self.service = service
        self.local_ip = local_ip  # this computer's address as the router sees it

    def __repr__(self):
        return "Gateway(%r, %r)" % (self.control_url, self.service)


class Mapping:
    def __init__(self, gateway, port, external_ip, lease):
        self.gateway = gateway
        self.port = port
        self.internal_ip = gateway.local_ip
        self.external_ip = external_ip  # None if the router didn't say
        self.lease = lease              # 0: until it is removed


class PortMapper:
    """Finds the router and maps a TCP port to this computer. `ssdp` and
    `sources` are for the tests: where to send the search, and from which
    local addresses (one socket each; "" lets Windows choose)."""

    def __init__(self, ssdp=SSDP, sources=("",), search_time=2.5, timeout=4.0):
        self.ssdp = ssdp
        self.sources = sources
        self.search_time = search_time
        self.timeout = timeout

    def open(self, port, description=DESCRIPTION):
        locations = search(self.ssdp, self.sources, self.search_time)
        if not locations:
            raise UpnpError("ingen router svarade på UPnP (det kan vara avstängt i routern)")
        problems = []
        for location in locations:
            try:
                gateway = find_gateway(location, self.timeout)
                lease = add_mapping(gateway, port, description, self.timeout)
            except UpnpError as e:
                problems.append(e)
                continue
            try:
                external = external_ip(gateway, self.timeout)
            except UpnpError:
                external = None
            return Mapping(gateway, port, external, lease)
        # The most telling problem: one a router stated, rather than one that didn't answer.
        problems.sort(key=lambda e: e.code is None)
        raise problems[0]

    def renew(self, mapping, description=DESCRIPTION):
        mapping.lease = add_mapping(mapping.gateway, mapping.port, description, self.timeout)

    def close(self, mapping):
        try:
            soap(mapping.gateway, "DeletePortMapping", [("NewRemoteHost", ""), ("NewExternalPort", mapping.port),
                                                        ("NewProtocol", "TCP")], self.timeout)
        except UpnpError as e:
            if e.code != NO_SUCH_ENTRY:
                raise


# --- discovery ---------------------------------------------------------------------------

def search_message(target):
    return ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
            "ST: %s\r\n\r\n" % target).encode("ascii")


def search(ssdp=SSDP, sources=("",), search_time=2.5):
    """The description URLs of the gateways that answer an M-SEARCH, in the
    order they answered. Waits at most `search_time`, and a moment after the
    first answer."""
    sockets = []
    for source in sources:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            if source:
                sock.bind((source, 0))
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                if source:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source))
            except OSError:
                pass  # e.g. no multicast on that interface: a unicast search still works
            for target in TARGETS:
                sock.sendto(search_message(target), ssdp)
        except OSError:
            sock.close()
            continue
        sockets.append(sock)
    locations = []
    deadline = time.monotonic() + search_time
    try:
        while sockets:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            readable, _, _ = select.select(sockets, [], [], left)
            for sock in readable:
                try:
                    data, _ = sock.recvfrom(65535)
                except OSError:
                    continue
                location = answer_location(data)
                if location and location not in locations:
                    locations.append(location)
                    deadline = min(deadline, time.monotonic() + 0.5)  # others answer about as fast
    finally:
        for sock in sockets:
            sock.close()
    return locations


def answer_location(data):
    """The LOCATION of an SSDP answer from a gateway, if it is one we may ask."""
    try:
        lines = data.decode("latin-1").split("\r\n")
    except ValueError:
        return None
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return None
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().upper()] = value.strip()
    target = headers.get("ST", "")
    if target and not any(kind in target for kind in ("InternetGatewayDevice", "WANIPConnection",
                                                       "WANPPPConnection")):
        return None
    location = headers.get("LOCATION")
    return location if location and local_url(location) else None


def local_url(url):
    """Only plain http to an address on this network: an answer to a
    multicast search could name any URL, and nothing else is a router."""
    try:
        parts = urllib.parse.urlsplit(url)
        address = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        return False
    return parts.scheme == "http" and (address.is_private or address.is_loopback or address.is_link_local)


# --- the gateway's services ----------------------------------------------------------------

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the router is never behind a proxy


def http(request, timeout):
    """(status, body) of a request to the router, or UpnpError if it didn't answer."""
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_XML)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(MAX_XML)
        finally:
            e.close()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpnpError("routern svarade inte (%s)" % (getattr(e, "reason", None) or e)) from None


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise UpnpError("routern svarade med trasig XML") from None


def find_gateway(location, timeout=4.0):
    status, data = http(urllib.request.Request(location), timeout)
    if status != 200:
        raise UpnpError("routern gav ingen beskrivning (HTTP %d)" % status)
    root = parse_xml(data)
    base = location
    for element in root.iter():
        if local_name(element.tag) == "URLBase" and (element.text or "").strip():
            base = element.text.strip()
    found = []
    for element in root.iter():
        if local_name(element.tag) != "service":
            continue
        fields = {local_name(child.tag): (child.text or "").strip() for child in element}
        service, control = fields.get("serviceType", ""), fields.get("controlURL", "")
        if service.startswith(SERVICES) and control:
            url = urllib.parse.urljoin(base, control)
            if local_url(url):
                found.append((service, url))
    if not found:
        raise UpnpError("routern har ingen UPnP-tjänst för portöppning")
    host = urllib.parse.urlsplit(found[0][1])
    local = address_towards(host.hostname, host.port or 80)
    gateways = [Gateway(url, service, local) for service, url in found]
    if len(gateways) > 1:
        # A router may list both kinds; the one with an address is the connected one.
        for gateway in gateways:
            try:
                if external_ip(gateway, timeout):
                    return gateway
            except UpnpError:
                continue
    return gateways[0]


def address_towards(host, port):
    """This computer's address on the way to `host` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((host, port))
            return sock.getsockname()[0]
        except OSError:
            raise UpnpError("når inte routern på %s" % host) from None


def soap(gateway, action, args=(), timeout=4.0):
    """Call `action` on the gateway's service: the answer's values by name, or
    UpnpError with the router's error code."""
    body = ('<?xml version="1.0"?>\r\n<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="%s">%s</u:%s>'
            '</s:Body></s:Envelope>\r\n' % (action, gateway.service,
                                             "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in args),
                                             action))
    request = urllib.request.Request(gateway.control_url, data=body.encode("utf-8"), method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": '"%s#%s"' % (gateway.service, action)})
    status, data = http(request, timeout)
    root = parse_xml(data) if data else None
    values = {} if root is None else {local_name(e.tag): (e.text or "").strip() for e in root.iter()}
    if status != 200:
        code = values.get("errorCode")
        code = int(code) if code and code.isdigit() else None
        raise UpnpError(router_problem(code, values.get("errorDescription") or "HTTP %d" % status), code)
    return values


def router_problem(code, description):
    if code == NOT_AUTHORIZED:
        return "routern tillåter inte att program öppnar portar (UPnP är låst i routern)"
    return "routern sa nej (%s%s)" % ("fel %d: " % code if code else "", description)


def external_ip(gateway, timeout=4.0):
    address = soap(gateway, "GetExternalIPAddress", (), timeout).get("NewExternalIPAddress") or None
    try:
        return str(ipaddress.IPv4Address(address)) if address else None
    except ValueError:
        return None


def mapping_entry(gateway, port, timeout=4.0):
    """Who a port is mapped to now: {client, port, description}, or None."""
    try:
        values = soap(gateway, "GetSpecificPortMappingEntry", [("NewRemoteHost", ""), ("NewExternalPort", port),
                                                              ("NewProtocol", "TCP")], timeout)
    except UpnpError as e:
        if e.code == NO_SUCH_ENTRY:
            return None
        raise
    return {"client": values.get("NewInternalClient"), "port": values.get("NewInternalPort"),
            "description": values.get("NewPortMappingDescription")}


def add_mapping(gateway, port, description=DESCRIPTION, timeout=4.0, lease=LEASE):
    """Map TCP `port` on the router to the same port here. Returns the lease
    it got (0: until removed); UpnpError if the router refuses."""
    retried = False
    while True:
        args = [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", "TCP"),
                ("NewInternalPort", port), ("NewInternalClient", gateway.local_ip), ("NewEnabled", 1),
                ("NewPortMappingDescription", description), ("NewLeaseDuration", lease)]
        try:
            soap(gateway, "AddPortMapping", args, timeout)
            return lease
        except UpnpError as e:
            if e.code == PERMANENT_ONLY and lease:
                lease = 0  # an older router: only mappings without an end
                continue
            if e.code != CONFLICT or retried:
                raise
            entry = mapping_entry(gateway, port, timeout)
            if entry and entry["client"] and entry["client"] != gateway.local_ip:
                raise UpnpError("routern skickar redan port %d till en annan dator (%s)" % (port, entry["client"]),
                                CONFLICT) from None
            # Ours, left over from before: replace it.
            retried = True
            try:
                soap(gateway, "DeletePortMapping", [("NewRemoteHost", ""), ("NewExternalPort", port),
                                                    ("NewProtocol", "TCP")], timeout)
            except UpnpError:
                pass
