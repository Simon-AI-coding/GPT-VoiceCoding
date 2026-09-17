# 28. The Duty Card is the at-computer surface; the Control Panel is a window behind it, and the menu bar is a menu

Date: 2026-09-10 · Status: Accepted · Source: #340 (map #333)

The v0 shell is one `MenuBarExtra` dropdown, 340 points wide, and it is the
whole Control Panel: switches, Live Toggle, roster, engine health, stderr,
seam table and socket path in one column. That fit a maintainer; it does not
fit a stranger. The redesign adds a deep Settings module, a Diagnostics page,
a second-level Session list and a first-launch flow, and the requirement's
first line is that the user sees what matters **without clicking anything**.

Three carriers were on the table: the dropdown alone; a menu-bar item plus a
main window; a Dock application. All three put the glance behind a click on
the menu bar, and a menu-bar title is the wrong place for it — a full menu bar
hides extras with no way to tell (#339), and a text label is the first to go.
The decision is a fourth shape, taken from the maintainer's own sketch:

1. **The Duty Card** is the primary surface: a small always-on-top panel that
   sits on the desktop, over every Space and full-screen app, and never takes
   focus. It exists exactly while the Duty Switch is on — Duty off withdraws
   it, and there is no separate hide. It carries the few facts the user must
   see at a glance and is dynamic: the Live Call's state, the count of main
   Sessions waiting on the user (a decision or a permission) and the count
   finished, and the newest such Session's one-line brief. A click on it
   offers the Live Toggle and two doors — the Control Panel's home and its
   Settings; a secondary click offers Quit. What the line carries in what
   order, and its states' exact mapping to the Companion Channel's words, are
   the Duty Card's own ticket (#343).
2. **The Control Panel** keeps its name and becomes a real window, opened
   from the Card or the menu bar: one window, a Home section and a Settings
   section, Diagnostics under Settings. Home holds the Duty Switch, the Live
   Toggle with the call's thread id and the Voice Switch beside it, the
   Message Switch worded as Telegram connected / disconnected, the Call
   Agent's model, effort, context and a fresh-agent action, the four counts
   (Sessions, waiting on the user, finished, Child Processes) and under them
   the Session list. Nothing technical shows on Home; Diagnostics holds it.
3. **The menu bar** is a plain four-item menu — open the Control Panel,
   Settings, the Duty Switch, Quit — behind a static icon that at most dims
   with Duty. It shows no counts and no call state; the Card does. So the
   status item stays `MenuBarExtra`, in `.menu` style; a hand-rolled
   `NSStatusItem` was needed only for a click-to-toggle bar item (#339), and
   the Card takes that click instead.

The app stays a menu-bar application (`LSUIElement`): no Dock icon at rest.
While the Control Panel window is open the app joins the Dock and ⌘-Tab, so a
covered window can be found again, and leaves them when the window closes.
First launch opens the Control Panel on the first-launch flow; every later
launch, login items included, is silent — the icon, and the Card if Duty is on.

## Considered and rejected

- **Dropdown alone** — cannot hold Settings, a list, Diagnostics or a
  first-launch flow; and everything is behind a click.
- **Menu-bar title as the glance** — the counts would be hidden on a crowded
  bar without the app knowing, and a click-to-toggle item forces `NSStatusItem`.
- **Dock application** — contradicts what the product is: a bridge that
  stays up behind the user's terminals, not a tool one opens to use.
- **A hide switch for the Card while Duty is on** — a second switch for one
  notion; the Duty Switch already says whether the system is on duty.
- **Voice Switch and Auto Hang-up Switch on Home** — the Voice Switch sits
  with the Live Toggle it governs; Auto Hang-up is rarely flipped and lives
  in Settings.

## Consequences

- Two charted decisions on map #333 are superseded: the menu-bar title no
  longer shows the counts, and the floating strip is no longer an optional
  feature — it is the Duty Card, and the Duty Switch is its only switch.
- The Duty Switch gains a visible consequence on the Control Panel (the Card
  appears and withdraws with it); `CONTEXT.md` says so.
- An always-visible Card keeps the app out of App Nap (#339). The shell owns
  one poller for every surface; the Card's cadence is a design cost the
  Settings and Duty Card tickets weigh, not a reason to move the glance.
- `waiting on {name}` is not counted on the Card — it is a Session waiting
  on another Session, not on the user — and appears only in the Session list.
- The Control Panel's definition in `CONTEXT.md` widens from "the dropdown"
  to the window; the v0 dropdown's contents are redistributed, and nothing
  the shell showed is lost — technical text moves to Diagnostics.

## Amendment — Duty Lamp (#363, 2026-09-15)

The **Duty Lamp** replaces the Duty Card's persistent two-line presentation.
It is the same desktop surface and Duty remains its only visibility switch.
The fixed 72×26 plate carries the Call Phase as a glyph and fill, waiting and
finished counts as numbers, and alternates those numbers with call duration
every three seconds. The Session line is now a 280-point bubble: on hover it
reads the most recently active counted row; automatically it displays only
the latest eligible Stop snapshot supplied by Core for five seconds. A newer
reminder replaces it and restarts the hold; hovering keeps it open.

Core retains one ephemeral reminder with a unique identity and its row snapshot
in the existing Roster Brief. Reading it does not create a new reminder. Duty
off or an ended target clears it. The shell establishes a baseline on first
read, Duty reopening and engine reconnection, rather than replaying that record.
There is no notification queue, acknowledgement, new query action or new poller.
Core remains the authority for classifications and recent-activity order.

Click expands the existing actions leftward without moving the Lamp; secondary
click offers Quit in the same location. Hover, its crossing gap and the bubble
share one reading region; the Lamp uses a pointing-hand cursor. The nonactivating
window itself is 72×26 and saved old Card dimensions are normalized while keeping
the top-right anchor. Quit during a call retains the existing confirmation.

General adds a shell-local System / Dark / Light preference, applied immediately
to the main window, Lamp and its attachments. It does not write engine settings
or request a restart. Other decisions above remain in force. The fixed handoff
listed in #363 is the visual authority, including both appearances and motion.


## Amendment — Duty Lamp follow-up (#364, 2026-09-15)

The root Stage 2 in the fixed follow-up handoff is the visual authority. The
right slot opens Call / Hang up, Control and Settings; the left cell dials when
Ready, cancels its pending dial during Calling, and asks before hanging up an
established call. Hover and the pending ask use the design's red 135-degree
handset; Keep, Escape, another left-cell click or six seconds cancels the ask.
Explicit Hang up buttons retain their direct action. The plate remains 72×26.

DesktopWindows owns one native scrolling window, measured at its current width.
Opening it from the Lamp anchors it below and right-aligned on that display;
internal navigation preserves the top edge. Content taller than that display
scrolls. The initial content is measured before the viewport is assigned, so a
zero-height viewport cannot prevent its own first measurement. Shared native
tracking regions provide the pointing hand even in inactive windows; dragging
the Lamp does not activate either click action.

Message times travel with ProgressEntry and Briefing's selected Newest, through
the roster, detail and immutable desktop reminder snapshot. Activity sorting is
unchanged. The Shell formats these times on its existing visible clock, with no
new backend reads or timer per row. Missing times remain absent and display an
em dash. Claude transcript timestamps supply message times. The installed
codex-cli 0.154.0 experimental thread/read schema omits message timestamps,
but its persisted item_completed records carry completed_at_ms and exact
thread/turn/item IDs. The Adapter reads the daemon-named rollout during its
existing deep read, validates the file's thread identity, and joins only times
for daemon-selected messages. The existing TurnCache retains that reading;
there is no new poller, content source, or arrival-time ledger. Missing source
records stay unknown rather than borrowing turn or activity times. A live
read on 2026-09-15 matched all 84 returned messages to their lifecycle records.
This narrowly extends the old no-rollout-progress rule: content, state and
approval authority remain with the app-server; the local record supplies only
its omitted message completion time. Historical files lacking those exact
lifecycle records cannot provide a verified time.


Cancellation extends the existing live control action with cancel_dial carrying
an opaque attempt ID. User dials may provide attempt_id; automatic dials receive
one in CallKeeper and status exposes the current dial_attempt. The Keeper sends
end_call outside the opening operation lock, sharing a completion future with
the opening attempt so the lock cannot admit a later dial before cleanup ends.
Stale or repeated cancellation cannot toggle a subsequent call. A queued CallStarted from a handshake whose result was cancelled cannot
re-adopt that released call. Other call event and cue semantics are unchanged. The existing Realtime Adapter owns connection/audio cleanup.

Language and engine configuration have independent pending-application state.
Restart now reloads Shell text directly for language-only changes; combined
changes reload that text after the replacement engine is reachable. Calling,
On a call and Ending disable the action without scheduling a later restart.
Appearance remains immediate and independent.

## Amendment — the Control Panel floats with the Lamp (2026-09-17)

Opened over a full-screen app, the Control Panel switched the user to another
Space: it was a normal window, and opening it made the app a regular one and
activated it, which macOS answers by moving to a Space where that window can
live. The Panel is now a floating panel that joins every Space and full-screen
app, as the Lamp does, and the app stays a menu-bar application while it is
open: opening it activates the app without making it a regular one, so it no
longer joins the Dock and the app switcher — a panel that floats cannot be
covered. It floats one level under the Lamp, so the Lamp, its strip and its
bubbles stay above it. A non-activating Panel was tried first and rejected:
with the app activated by the Lamp click, a click on that Panel handed
activation back to the front app, so the click reached the monitor for other
applications' clicks, in the Panel's own coordinates, and closed the Panel
under the user's hand (logged 2026-09-17). ADR 0029's interface and height
rule are unchanged; its title bar stays for the width resize, without buttons,
and the content runs under it from the top edge, so the window is exactly as
tall as its content.

Every door opens the same panel, anchored below and right-aligned to the
Lamp's place — its saved place while Duty is off. Once shown it is the user's
to drag by any part that is not a control (by its top edge alone before macOS
15), apart from the Lamp: dragging either leaves the other where it is, and
navigating inside it keeps its place. It closes on a second Lamp click, on
Escape (after any confirmation Escape cancels first), or when the app stops
being active — the user clicked another application. No click monitor decides
it; clicks inside this app, its menus included, never close it. Closing it
from outside cancels a Telegram binding in progress, as any close does.

Hover over any part of the Lamp expands Call / Hang up, Control and Settings
to its left; the strip and the gap before it belong to the reading region, so
travelling onto the strip keeps it open. While the Lamp is dragged, the strip
and every bubble are withdrawn — carried along they made the drag stutter —
and they return beside the Lamp where it lands. A click on the right slot
opens or closes the Control Panel; the left cell and the secondary click are
unchanged. While the Panel is open the Lamp shows no Session bubble — neither
the hover row, nor a reminder, nor a failed dial's notice; a reminder that
arrives then is not replayed later. The hang-up and quit asks and the engine
notice still show, above the Panel.
