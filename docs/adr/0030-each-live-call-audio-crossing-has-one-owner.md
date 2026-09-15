# 30. Each Live Call audio crossing has one owner: the speaker keeps listening continuous, the microphone keeps speaking fresh

Date: 2026-09-15 · Status: Accepted · Source: #365

A Live Call's audio crosses between a device clock and the call's clock twice, and neither crossing
had an owner. **Listening:** inbound frames arrive on the network's schedule, the output device
takes a 20 ms block on its own, and between them sat a bare byte buffer that played silence for
whatever was a few milliseconds late. A speech waveform cut to zero for 20 ms is a click. **Speaking:**
the microphone opens when the call attempt is registered, about six seconds before the far side can
hear, and captured frames piled into a two-second queue that aiortc's sender, which pulls as fast as
the track yields, sent all at once when the connection came up — ahead of the user's live words.

The evidence, all measured: across 8 silent calls, 0 RTP packets missing and 0 jitter-buffer
discards, yet replaying each call's arrival times through the bare buffer gives mid-speech
zero-fills in 7 of 8. Arrival jitter p99 was 24–31 ms, with occasional 60–90 ms holds followed by a
catch-up burst. In two instrumented calls the user heard noise on two specific words, and each had
a 20 ms mid-speech zero-fill inside that utterance and no other anomaly. In both, the sender's first
pull found 100 frames queued and 201–213 already dropped.

## Decision

**Each crossing is owned by the class that already sits on it**, deepened in place and private to
`webrtc.py`. Neither interface grows. The two rules are opposite, so they are not merged.

**The speaker owns listening, and trades delay for continuity.**

- *Headroom is measured.* A frame's lateness is how far behind the stream's own pace it arrived.
  Headroom is the widest lateness seen above the earliest recent frame, and it never shrinks during
  a call. *Recent* means the ceiling plus one frame, counted back from the frame before: any hold
  the headroom can cover has caught up by then, so lateness that lasts longer is the pace moving
  (the far side paused), and the pace follows it.
- *Headroom has a ceiling, and it is the product's delay limit, not a tuning:* `MAX_HEADROOM_SECONDS`,
  four frames (80 ms). That is the most whole frames under a hundred milliseconds, because a live
  conversation tolerates tens of milliseconds. The added delay a user hears is at most that plus the
  one block the device's own clock costs.
- *Headroom is held against the pace, not the buffer's instantaneous level.* The level jumps by a
  frame with each arrival's phase. Aiming that level at a target set the delay by whichever frame
  happened to be late when the device looked. Measured from each speech frame's arrival over the
  recordings, that version reached 110–117 ms of delay, and this one 100.0–100.4 ms. The speaker
  counts the audio it holds plus the audio the pace says is owed but still in flight.
- *It changes only where it cannot be heard:* between a device block that ended at zero and a whole
  frame of digital silence. The Voice is known not to be speaking without any level being picked.
  Zero is not a threshold, and a whole 20 ms of exact zeros never occurs inside speech. Opus encodes
  digital silence as 3-byte packets that decode to exact zero frames (checked against libopus).
- *A dry spell is concealed, never cut.* A block the buffer cannot fill fades linearly to zero across
  itself. Its missing part holds the last real sample, so the fade starts where the audio was.
  Playback then waits until the headroom has refilled, or for as long as the headroom lasts if
  nothing more arrives, so a short last utterance is never held back. It resumes with a linear fade
  in across one block. One block is long enough that the ramp is not a step, and it is the block
  that was going to be wrong anyway.
- *The two-second ceiling stays:* overflow still drops the oldest audio.
- Headroom is held as buffered audio, so ADR 0027's span already counts it. The Playout record is
  unchanged.

**The microphone owns speaking, and trades completeness for freshness.**

- *The sender's first pull is the moment the far side can hear.* Everything captured before it is
  dropped. The microphone still opens when the attempt is registered, so echo cancellation warms up
  exactly as early as before.
- *After that the sender may fall behind by `MAX_SENDER_LAG_FRAMES`, two frames:* one for each thread
  handoff between the capture callback and the sender (callback to echo-cancellation worker, worker
  to event loop), because each can hold a frame at the moment of the pull. Anything beyond that is
  backlog. The oldest frames are dropped, so a sender that stalled resumes on current audio and
  never on a burst.
- Every dropped frame is counted and logged when capture stops.

## What the recordings showed, and the limit they forced

Three recorded calls (`tests/realtime_call_arrivals/`) are replayed through the real device callback
at eight device-clock phases. Under the bare buffer, every recording cut speech. Under this rule, none
does, and the worst added delay is 99.5–99.99 ms against the 100 ms bound.

The ceiling binds on every call. All three recordings share a far-side hold of ~200 ms at frame 86,
before the Voice speaks. One recording (221301) has a 72 ms network hold with a catch-up burst in
the middle of speech, which needs about 80 ms of headroom to cover. Nothing measured earlier in that
call predicts it except the startup hold. A simulation over the recordings with the same silence
gating (the capped row is this implementation, through the test replay):

| Headroom from | Cuts in 221301 / 221354 / 221448 (8 phases) | Delay |
|---|---|---|
| none (bare buffer) | 28 / 7 / 7 | — |
| recent catch-up only (~10–20 ms) | 28 / 7 / 7 | ~20 ms |
| a fixed 60 ms | 1 / 0 / 0 | ~60 ms |
| the widest lateness, capped at 80 ms | 0 / 0 / 0 | ≤ 80 ms + one block |
| the widest lateness, uncapped | 0 / 0 / 0 | ~200–230 ms |

So headroom that is purely measured either cuts or adds a fifth of a second, and the limit is where
the product's own delay allowance decides it.

## Rejected

- **A fixed prebuffer, even re-primed when empty.** Over the recordings the cuts were non-monotonic in
  its size, because the far side's stream runs slightly slower than the device and pre-speech gaps
  drain a fixed reserve. The reserve has to be restored during silence, which a fixed prebuffer
  cannot do.
- **Fade-only concealment on the bare buffer.** It turns each click into an audible dip and removes
  none of them. The bare buffer's 1–4 mid-speech shortfalls per recording would all remain.
- **New buffer modules, one per direction.** Each would have one caller and no seam of its own. The
  tests cross at the speaker and the microphone.
- **One component with a mode switch.** It would put two opposite policies behind one interface that
  neither caller needs.
- **An audio-energy threshold for "the Voice is silent".** Any level would be one machine's number.
  Digital silence needs none.
- **Platform voice processing** — already rejected by ADR 0025.

## What this rests on, and how to check it

The far side's silence is digital silence. If it is not, headroom is never adjusted mid-stream. It
is still set at the start and whenever the buffer refills after a dry spell (the startup hold is
one), but slow drift is corrected only at the next dry spell, which is concealed rather than cut.
Manual acceptance is a few real calls with speakers and a microphone: no mid-speech clicks, and the
first user transcript after the Connected Cue matches what was said live. Whether the stale capture
burst caused the garbled first transcript seen during diagnosis is to be verified from real calls
after this lands.
