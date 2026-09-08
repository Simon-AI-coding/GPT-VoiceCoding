"""The bridge-owned realtime call, and the factory a configuration file names.

`config.toml` points at `gpt_voicecoding.adapters.call.realtime:realtime_call`,
and the composition root calls it with the event sink and this seam's settings
table. Nothing else imports an adapter (ADR 0001).

The factory is also where the voice extra is proved to be installed. Doing it
here — while the engine is still being assembled — is what turns "configured but
not loadable" into a refusal to start, rather than into an outage the user
discovers at the moment they try to talk.
"""

from __future__ import annotations

from typing import Any

from gpt_voicecoding.adapters.call.realtime.adapter import (
    APPROVAL_POLICY,
    CODEX_RESPONSE_ITEM_PREFIX,
    CODEX_RESPONSES_AS_ITEMS,
    DELEGATION_ACK_FILLER,
    INCLUDE_STARTUP_CONTEXT,
    SANDBOX,
    TURN_FAILED,
    TURN_NOT_STARTED,
    USER_QUIET_POLL_FRACTION,
    DelegatedTurnError,
    RealtimeCallAdapter,
    ThreadGoneError,
)
from gpt_voicecoding.adapters.call.realtime.settings import (
    DEFAULT_REALTIME_MODEL,
    RealtimeCallSettings,
    SettingsError,
)
from gpt_voicecoding.adapters.call.realtime.transport import (
    CallTransport,
    CueOutput,
    TransportError,
    TransportFactory,
)

__all__ = [
    "APPROVAL_POLICY",
    "CODEX_RESPONSES_AS_ITEMS",
    "CODEX_RESPONSE_ITEM_PREFIX",
    "DEFAULT_REALTIME_MODEL",
    "DELEGATION_ACK_FILLER",
    "INCLUDE_STARTUP_CONTEXT",
    "SANDBOX",
    "TURN_FAILED",
    "TURN_NOT_STARTED",
    "USER_QUIET_POLL_FRACTION",
    "CallTransport",
    "DelegatedTurnError",
    "ThreadGoneError",
    "RealtimeCallAdapter",
    "RealtimeCallSettings",
    "SettingsError",
    "TransportError",
    "TransportFactory",
    "realtime_call",
]


def realtime_call(
    *,
    delegated_turn_model: str,
    sink: Any = None,
    settings: dict[str, Any] | None = None,
    transport_factory: TransportFactory | None = None,
) -> RealtimeCallAdapter:
    """Build the adapter from an opaque settings table, refusing keys it lacks.

    `delegated_turn_model` arrives beside the table rather than inside it, and
    the difference is the point (#270): it is `[delegate] model`, the engine's
    own setting, and the Call Agent is given the same value every Delegated Turn
    is given. A key in `[adapters.settings.call]` would be that one value stated
    twice, free to drift; it has no default here for the same reason it has none
    in `config.py`.
    """
    read = RealtimeCallSettings.of(settings)
    audio, cues = (transport_factory, None) if transport_factory else _audio_from(read)
    return RealtimeCallAdapter(
        delegated_turn_model=delegated_turn_model,
        sink=sink,
        settings=read,
        transport_factory=audio,
        cue_player=cues,
    )


def _audio_from(settings: RealtimeCallSettings) -> tuple[TransportFactory, CueOutput]:
    """The real audio path, with the voice extra proved present before it is needed."""
    from gpt_voicecoding.adapters.call.realtime import webrtc

    return webrtc.call_audio(
        input_device=settings.input_device, output_device=settings.output_device
    )
