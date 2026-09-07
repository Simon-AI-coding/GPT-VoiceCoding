"""The client half of RFC 6455, with no transport in it at all.

This repository ships with no dependencies, so it frames its own WebSocket. For
most of this product's life there was one place that did it —
`adapters/codex_app_server/wire.py` — and one place is not a duplication
problem. #272 made it two: `bridge-install status` has to complete an
`initialize` handshake on the shared app-server's control socket, and ADR 0012
forbids installation importing any adapter, so it cannot reach that client.

**So the protocol moved down here and the transport did not.** What is shared is
the part that is the same for everybody: the upgrade request, the accept-key
proof, and how a frame's bytes are laid out. What is *not* shared is reading and
writing them — the engine's client is `asyncio` because it lives inside the
engine's loop, and installation's is a blocking socket because it runs before
any loop exists. Trying to share the I/O too would mean one of the two callers
holding a transport shaped for the other, and this module deliberately holds
neither: nothing here opens, reads or writes anything.

**What is shared goes as far as the opcodes, and stops at the wait.** Both
callers reassemble a message the same way — skip a pong, answer a ping, gather
data frames and continuations until the final one — and that reassembly is
protocol, not transport. It lives here as :class:`Reassembly`, which is fed
decoded frames and says what to do about each; what it never does is *get* one.
Splitting it there is what lets one caller await its frames and the other block
for them while the rule about which opcode means what is written once.

**Nothing here words an error about codex, either.** Both callers phrase their
own refusals, because "the codex app-server set reserved WebSocket bits" is the
engine's sentence about its own far side and a leaf that owned it would be
speaking for callers it does not know. What the leaf owns is the *fact* — which
bits are reserved, what the accept key must be — so no caller has to spell it.

Legacy: **not ported.** `legacy@1d32845` drove a per-Session app-server over a
socket it owned and framed nothing of its own.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from typing import Final

#: RFC 6455's fixed accept-key salt. Not a secret; the handshake is a proof of
#: protocol, not of identity.
WEBSOCKET_GUID: Final = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION: Final = 0x0
OPCODE_TEXT: Final = 0x1
OPCODE_BINARY: Final = 0x2
OPCODE_CLOSE: Final = 0x8
OPCODE_PING: Final = 0x9
OPCODE_PONG: Final = 0xA

#: The three bits RFC 6455 reserves and this client has no extension for. A peer
#: that sets one is speaking something this is not, and each caller says so in
#: its own words.
RESERVED_BITS: Final = 0x70

#: How many bytes a mask is. Fixed by the RFC, and spelled here so no read loop
#: has a bare `4` in it.
MASK_BYTES: Final = 4

#: What ends the upgrade's header block, and what a caller reads up to.
UPGRADE_TERMINATOR: Final = b"\r\n\r\n"

#: The only status line that means the upgrade happened, matched with its
#: surrounding spaces so a `101` inside a reason phrase cannot pass for it.
ACCEPTED_STATUS: Final = " 101 "

#: The header carrying the peer's proof, casefolded the way `upgrade_answer`
#: returns them.
ACCEPT_HEADER: Final = "sec-websocket-accept"

#: How long the key a client offers is, before base64. The RFC's number.
KEY_BYTES: Final = 16

#: Where a frame's length stops fitting in the header's own seven bits, and how
#: many bytes name it instead. Read as a table rather than as three branches
#: repeated on both the writing and the reading side.
SHORT_LENGTH_LIMIT: Final = 125
TWO_BYTE_LENGTH_MARKER: Final = 126
EIGHT_BYTE_LENGTH_MARKER: Final = 127
TWO_BYTE_LENGTH_LIMIT: Final = 0xFFFF


def upgrade_request(key: str | None = None) -> tuple[bytes, str]:
    """The bytes to send, and the key the answer has to be checked against.

    Both together, because they are one act: a caller that generated the key
    separately could send one and verify another, and the check would then pass
    on a peer that echoed nothing. `key` is a parameter only so a test can pin
    the exchange; every real caller lets this make one.
    """
    offered = key or base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")
    request = (
        "GET / HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {offered}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    return request, offered


def upgrade_answer(header: bytes) -> tuple[str, dict[str, str]]:
    """One header block, read as its status line and its headers.

    `iso-8859-1` rather than UTF-8 because it is the one decoding that cannot
    fail: an HTTP header block is bytes, a peer that is not an app-server may
    put anything in it, and a caller trying to explain a refusal must not be
    stopped by a decode error on the very bytes it wants to quote.

    Header names are casefolded because HTTP's are case-insensitive. Values are
    not touched beyond their surrounding whitespace — an accept key compared
    after any other change is not the key the peer sent.
    """
    lines = header.decode("iso-8859-1").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().casefold()] = value.strip()
    return (lines[0] if lines else ""), headers


def accept_key(key: str) -> str:
    """What a peer must answer to prove it read the key rather than guessed it."""
    return base64.b64encode(
        hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()  # noqa: S324
    ).decode("ascii")


def accepted(header: bytes, key: str) -> bool:
    """Whether one header block is an upgrade this client offered and won."""
    status, headers = upgrade_answer(header)
    return ACCEPTED_STATUS in status and headers.get(ACCEPT_HEADER) == accept_key(key)


def client_frame(payload: bytes, opcode: int) -> bytes:
    """One masked client frame. A client always masks; a server never does."""
    mask = os.urandom(MASK_BYTES)
    length = len(payload)
    header = bytearray((0x80 | opcode,))
    if length <= SHORT_LENGTH_LIMIT:
        header.append(0x80 | length)
    elif length <= TWO_BYTE_LENGTH_LIMIT:
        header.append(0x80 | TWO_BYTE_LENGTH_MARKER)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | EIGHT_BYTE_LENGTH_MARKER)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + mask + unmask(payload, mask)


@dataclass(frozen=True, slots=True)
class FramePrefix:
    """What a frame's first two bytes say, and what is still owed after them.

    `length` is the whole length only when ``extended_length_bytes`` is zero.
    Otherwise it is not known yet, and the caller reads that many more bytes and
    passes them to :func:`extended_length` — which is the shape a read loop
    needs, because the two transports that use this differ in *how* they read
    and agree exactly on *how much*.
    """

    opcode: int
    final: bool
    masked: bool
    reserved: bool
    length: int
    extended_length_bytes: int


def frame_prefix(first: int, second: int) -> FramePrefix:
    """Decode the two bytes every frame starts with. Judges nothing."""
    length = second & 0x7F
    if length == TWO_BYTE_LENGTH_MARKER:
        extended = 2
        length = 0
    elif length == EIGHT_BYTE_LENGTH_MARKER:
        extended = 8
        length = 0
    else:
        extended = 0
    return FramePrefix(
        opcode=first & 0x0F,
        final=bool(first & 0x80),
        masked=bool(second & 0x80),
        reserved=bool(first & RESERVED_BITS),
        length=length,
        extended_length_bytes=extended,
    )


def extended_length(raw: bytes) -> int:
    """The length those extra bytes name, however many of them there were."""
    return int(struct.unpack("!H" if len(raw) == 2 else "!Q", raw)[0])


def unmask(payload: bytes, mask: bytes) -> bytes:
    """Apply a frame's mask. Its own inverse, which is why writing uses it too."""
    return bytes(byte ^ mask[index % MASK_BYTES] for index, byte in enumerate(payload))


@dataclass(frozen=True, slots=True)
class Step:
    """What a reassembler makes of one frame: at most one of these is set.

    Three outcomes and no fourth. `pong_with` is a frame the caller must send
    back, because a peer that pings a client which never pongs is entitled to
    hang up; `message` is a whole message, reassembled; `refuse` is a reason
    this is not a peer speaking the protocol. All three unset means "read
    another frame" — a pong to ignore, or a fragment held for the next one.
    """

    message: bytes | None = None
    pong_with: bytes | None = None
    refuse: str | None = None


class Reassembly:
    """One message, gathered across frames. Reads nothing and writes nothing.

    Fed `(opcode, final, payload)` — however the caller got them — and answers
    a :class:`Step`. It holds only the fragments of the message being gathered,
    so one instance reads one message and is then done with.

    `limit` is the caller's own ceiling on a message, checked as the fragments
    grow rather than after: a peer that keeps sending continuations would
    otherwise be an unbounded buffer that is only noticed once it is full.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._fragments = bytearray()
        self._started = False

    def take(self, opcode: int, final: bool, payload: bytes) -> Step:
        if opcode == OPCODE_CLOSE:
            return Step(refuse="closed the WebSocket")
        if opcode == OPCODE_PING:
            return Step(pong_with=client_frame(payload, OPCODE_PONG))
        if opcode == OPCODE_PONG:
            return Step()
        if opcode in (OPCODE_TEXT, OPCODE_BINARY) and not self._started:
            self._started = True
        elif opcode != OPCODE_CONTINUATION or not self._started:
            return Step(refuse=f"sent an unexpected WebSocket opcode {opcode}")
        self._fragments.extend(payload)
        if len(self._fragments) > self._limit:
            return Step(refuse="sent a message past the configured frame limit")
        return Step(message=bytes(self._fragments)) if final else Step()
