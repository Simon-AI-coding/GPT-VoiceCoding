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
