# 27. The engine decides when the Voice's Playout has been heard, from the audio it already holds

Date: 2026-09-09 · Status: Accepted · Source: #301, replacing #235's unrecorded rule

A Playout span must end when the Voice has actually finished being heard, because
the engine holds every word it owes the user until then — a Session Brief, a Cue's
news, the Silence Ceiling's own measure of real silence. Recognise that end from
the audio this side is already holding: when the Voice stops generating, the bytes
still queued for the device are the whole of what it said that the user has yet
to hear, and that lasts exactly its own byte count divided by the stream's sample
rate and sample width.
The span ends that long later. No poll, no threshold, no fact about the far side.

Nothing the peer does decides it. #235 made the server's own end-of-playout event
the rule and kept inbound audio going quiet as the fallback; across 91 spans and
82 sessions the event fired zero times, because the whole `output_audio_buffer.*`
family belongs to the OpenAI Realtime API and not to the codex app-server wire —
which this repo's research had established four days before #235 installed it.
Quiet closed 84 spans and cannot close the rest: a peer that pads its stream with
silence never goes quiet, and its standing backlog of two to four frames means the
buffer-empty gate those rules sat behind never becomes true either. The gate is
removed with them; the span's own duration already accounts for every byte it
began with.

The bound stays as a genuine last resort rather than the everyday backstop, and
is now reachable only by a span holding more audio than the caller allowed time
for. The Playout record keeps every inbound measurement — frames, last-frame age,
largest gap, bytes buffered — as measurements rather than conditions, because they
are what made this diagnosable and are the sentinel for the next stall, and gains
one field: how much audio the span began with. The events data channel is still
read, for one purpose only: naming each event type it carries, once, so a codex
protocol change is noticed rather than silently absorbed.

The assumption this rests on is that no real speech audio arrives after the model
has said its turn is done. The run that produced the decision supports it — the
post-turn stream arrives at exactly real time with a flat backlog, which is a peer
generating silence live, not one flushing buffered speech — but it does not rule
out a short stretch of real speech in the first few hundred milliseconds, and
the device's own held block is uncounted by the same margin. A truncated answer
would show as a span that closed with a small starting figure, and the span-start
snapshot is where that would be widened.
