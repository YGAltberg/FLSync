"""Invite codes: what someone needs to join a host's session (its addresses,
port and password), as text that survives chat apps and being read aloud.

    FLS1-K3Q8-2B9T-...

After the prefix comes Crockford base32 (0-9 and A-Z without I, L, O, U) in
groups of four. Reading it back forgives case, spaces, dashes and the letters
O, I and L for 0, 1 and 1, and ignores text around the code.

The bytes inside:
    1 byte   number of addresses (high 4 bits); bit 0: a port follows
    2 bytes  the port, when it isn't 31415
    4 bytes  each address (IPv4), in the host's order
    8 bytes  the session's password
    2 bytes  CRC-16 of the bytes before it

The CRC catches every error within 16 bits in a row, so every single wrong
character (5 bits) and every swapped pair of neighbours (10 bits) is caught.
"""

import binascii
import ipaddress
import os
import re
import unicodedata

PREFIX = "FLS1"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford's base32
ALIASES = {"O": "0", "I": "1", "L": "1"}
DEFAULT_PORT = 31415
PASSWORD_BYTES = 8
MAX_ADDRESSES = 8
GROUP = 4
CUSTOM_PORT = 0x01
KNOWN_FLAGS = CUSTOM_PORT
MIN_CHARS = -(-(1 + 4 + PASSWORD_BYTES + 2) * 8 // 5)  # the shortest code: one address

LAN = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's addresses (also carrier NAT's)
LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
KINDS = ("lan", "tailscale", "internet")  # the order a joiner tries them in
KIND_NAMES = {"lan": "lokalt nätverk", "tailscale": "Tailscale", "internet": "internet", "local": "den här datorn",
              "other": "annan adress"}


class InviteError(ValueError):
    """The text isn't a usable invite code; the message says why (in Swedish)."""


class Invite:
    def __init__(self, addresses, port, password):
        self.addresses = list(addresses)  # IPv4 texts, in the host's order
        self.port = port
        self.password = password          # the relay's password (hex text)

    @property
    def code(self):
        return encode(self.addresses, self.port, bytes.fromhex(self.password))

    def __repr__(self):
        return "Invite(%r, %r)" % (self.addresses, self.port)


# --- addresses ------------------------------------------------------------------------

def kind(address):
    """'lan', 'tailscale', 'internet', 'local' (this computer) or 'other' for an IPv4 text."""
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return "other"
    if ip in LOOPBACK:
        return "local"
    if ip in TAILSCALE:
        return "tailscale"
    if any(ip in net for net in LAN):
        return "lan"
    if ip in LINK_LOCAL or not ip.is_global or ip.is_multicast:
        return "other"
    return "internet"


def is_public(address):
    return kind(address) == "internet"


def host_addresses(local, default=None, public=None, max_lan=2):
    """The addresses to put in an invite, in order: this computer's LAN
    addresses (the one its traffic leaves by first, at most `max_lan`), a
    Tailscale address, and public ones (an interface's, or the router's from
    UPnP or a lookup)."""
    lan, tailscale, internet = [], [], []
    for address in ([default] if default else []) + list(local) + ([public] if public else []):
        found = kind(address)
        if found == "lan" and address not in lan:
            lan.append(address)
        elif found == "tailscale" and address not in tailscale:
            tailscale.append(address)
        elif found == "internet" and address not in internet:
            internet.append(address)
    return lan[:max_lan] + tailscale[:1] + internet[:2]


def join_order(addresses):
    """The order a joiner tries an invite's addresses in: LAN (and this
    computer), Tailscale, internet; the host's order within each."""
    rank = {"local": 0, "lan": 0, "tailscale": 1, "internet": 2}
    return sorted(addresses, key=lambda a: rank.get(kind(a), 3))


# --- the code ---------------------------------------------------------------------------

def new_password():
    return os.urandom(PASSWORD_BYTES).hex()


def encode(addresses, port, password):
    """The invite code for these addresses (IPv4 texts), port and password (bytes)."""
    if not addresses:
        raise InviteError("inbjudan behöver minst en adress")
    if len(addresses) > MAX_ADDRESSES:
        raise InviteError("för många adresser")
    if len(password) != PASSWORD_BYTES:
        raise InviteError("lösenordet ska vara %d byte" % PASSWORD_BYTES)
    flags = 0 if port == DEFAULT_PORT else CUSTOM_PORT
    data = bytes([len(addresses) << 4 | flags])
    if flags & CUSTOM_PORT:
        data += int(port).to_bytes(2, "big")
    for address in addresses:
        data += ipaddress.IPv4Address(address).packed
    data += password
    data += crc(data).to_bytes(2, "big")
    text = to_base32(data)
    return "-".join([PREFIX] + [text[i:i + GROUP] for i in range(0, len(text), GROUP)])


def decode(text):
    """The Invite in `text`, or InviteError saying what's wrong with it."""
    chars, prefixed = _code_chars(text)
    if not chars:
        raise InviteError("Klistra in inbjudan du fick, den börjar med %s-" % PREFIX)
    if not prefixed and len(chars) < MIN_CHARS:
        raise InviteError("Det där ser inte ut som en FLSync-inbjudan. Den börjar med %s-." % PREFIX)
    if len(chars) < 2 or chars[0] not in ALPHABET or chars[1] not in ALPHABET:
        raise InviteError(_BROKEN)
    header = from_base32(chars[:2], check_padding=False)[0]
    count, flags = header >> 4, header & 0x0F
    if flags & ~KNOWN_FLAGS or not 0 < count <= MAX_ADDRESSES:
        raise InviteError(_BROKEN)  # a new format would get a new prefix
    size = 1 + (2 if flags & CUSTOM_PORT else 0) + 4 * count + PASSWORD_BYTES + 2
    needed = -(-size * 8 // 5)
    if len(chars) < needed:
        raise InviteError("Inbjudan är inte hel: det fattas %d tecken. Kopiera hela koden en gång till."
                          % (needed - len(chars)))
    for ch in chars[:needed]:
        if ch not in ALPHABET:
            raise InviteError("Inbjudan innehåller tecknet %s, som aldrig finns i en kod. %s" % (ch, _CHECK))
    try:
        data = from_base32(chars[:needed])
    except InviteError:
        raise InviteError(_BROKEN) from None
    if len(data) != size or crc(data[:-2]) != int.from_bytes(data[-2:], "big"):
        raise InviteError(_BROKEN)
    at = 1
    port = DEFAULT_PORT
    if flags & CUSTOM_PORT:
        port = int.from_bytes(data[1:3], "big")
        at = 3
    addresses = [str(ipaddress.IPv4Address(data[at + 4 * i:at + 4 * i + 4])) for i in range(count)]
    at += 4 * count
    if port == 0:
        raise InviteError(_BROKEN)
    return Invite(addresses, port, data[at:at + PASSWORD_BYTES].hex())


_CHECK = "Kontrollera att du har kopierat hela koden och att inget tecken är fel."
_BROKEN = "Inbjudan stämmer inte: något tecken är fel eller saknas. " + _CHECK


def _code_chars(text):
    """(the characters of the code itself, whether the prefix was there):
    upper case, separators and the prefix removed, look-alike letters made
    digits. Text after the code stays at the end (decode() ignores it)."""
    text = unicodedata.normalize("NFKC", text if isinstance(text, str) else "").upper()
    letters = "".join(ch for ch in text if ch.isascii() and ch.isalnum())
    found = re.search(r"FLS(\d)", letters)
    if found:
        if found.group(1) != PREFIX[-1]:
            raise InviteError("Inbjudan kommer från en annan version av FLSync (%s). Uppdatera FLSync hos er båda."
                              % found.group(0))
        letters = letters[found.end():]
    return "".join(ALIASES.get(ch, ch) for ch in letters), found is not None


def crc(data):
    return binascii.crc_hqx(data, 0xFFFF)  # CRC-16/CCITT


def to_base32(data):
    bits = len(data) * 8
    pad = -bits % 5
    value = int.from_bytes(data, "big") << pad
    count = (bits + pad) // 5
    return "".join(ALPHABET[(value >> (5 * (count - 1 - i))) & 31] for i in range(count))


def from_base32(chars, check_padding=True):
    value = 0
    for ch in chars:
        index = ALPHABET.find(ch)
        if index < 0:
            raise InviteError("ogiltigt tecken: %s" % ch)
        value = value << 5 | index
    bits = len(chars) * 5
    extra = bits % 8
    if check_padding and (extra >= 5 or value & ((1 << extra) - 1)):
        raise InviteError("fel längd")
    return (value >> extra).to_bytes(bits // 8, "big")
