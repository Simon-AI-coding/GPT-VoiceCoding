# 25. Capture removes the selected speaker's playback while keeping human speech

Date: 2026-09-08 · Status: Accepted · Source: #293, superseding #145's scope

Voice, Cues and other applications must remain audible without their acoustic
return becoming user intent. Keep sounddevice and aiortc in the existing audio
module; feed one local LiveKit echo canceller from a private, unmuted Core Audio
tap of the selected output device. A recording gate would also exclude human
interruptions. Platform voice processing would require replacing the existing
input and output ownership, whereas the tap gives this path an explicit reference
containing other applications too. Reference samples remain transient and local.

The audio module resolves the actual output device for each Live Call. Cues use
that same resolution while capture is alive, then return to play-time resolution
of the configured output after capture closes; their independent ended-call
lifetime remains. A device whose complete output cannot be represented by the
single device-targeted tap is an explicit audio-path error, never a partially
protected call. No capture-processing details become new domain vocabulary or
public Call settings.

Process taps raise the supported macOS minimum to 14.2 and require system-audio
recording permission in addition to microphone permission. Acquire it when opening
the call, not in the startup dependency probe; retain the existing verification
contract. Bundle/sign the native dependencies with the engine. Acoustic acceptance
and interruption grading remain separate from software and bundle checks.
