# 29. The Control Window owns its height; nothing outside it measures content

Date: 2026-09-15 · Status: Accepted

The handoff makes the Control Panel one normal window, 480 wide, **resizable sideways only**, and
says nothing in it scrolls but the roster. So the window's height is not the user's to set: it has
to follow its content. That rule had no owner. It lived in `DesktopWindows`, beside the Duty Lamp,
its attachments and the outside-click monitors, as two measurements:

1. a SwiftUI preference (`ContentHeight`) that reported the content's height to `resizeWindow`, and
2. a re-measure inside `synchronize()`, run whenever one of a hand-kept list of observed shell
   properties changed — added because the first path had been seen to miss changes.

Onboarding's Telegram step broke both. The binding flow adds rows to the same page, which changes
no property on the list, and the preference stopped arriving after the window's first layouts:
measured in a test, SwiftUI laid the content out at 457, 352 and 421 points while the callback never
fired again and the window stayed at 300. Content taller than its hosting view is centred, so the
step's header slid under the title bar and its footer — *Skip* and *Continue* — was cut off.
Adding the missing properties to the list, or a third measurement, would be the same shape again.

## Decision

**`ControlWindow` is the one module that knows the Control Panel's height.** Its interface is
`init(content:)`, `present(anchor:)`, `dismiss()` and `onClose`, and one invariant: *the content
area is as tall as the content's natural height, up to the screen's visible height, and only beyond
that does it scroll; the user can resize the width, never the height.* The hosting view, the scroll
view, the frame arithmetic, the height lock and the width hand-off are its implementation.

**Two rules, both inside it.** While the window is shown, the content's height is followed through
SwiftUI's `onGeometryChange`, which kept arriving in the same test where the preference did not.
When the window is presented, it measures once before ordering front, so a change made while it
was hidden cannot open it at a stale height. There is no list of properties to keep in step.

**`DesktopWindows` does not measure.** It decides from shell state *whether* the window is open and
*where* it anchors, and calls `present` or `dismiss`. The re-measure in `synchronize()` and the
`ContentHeight` preference are deleted, not kept as a fallback.

**Tests reach windows through the modules that own them.** `ControlWindow` is tested with a small
content view whose height changes, without a shell, an engine or a fixture. `DesktopWindows` lists
its own surfaces for its test; no test finds its windows by diffing `NSApp.windows`, which is what
made two window tests pick up each other's surfaces when they ran concurrently.

## Consequences

A height bug in the Control Panel now has one place to live and one test seam to reproduce it at.
A page that grows, shrinks or swaps content needs nothing from its author to keep the window right.
`onGeometryChange` is a SwiftUI API rather than a private one; if it too stops reporting in some
future host, the `ControlWindow` test is where that shows.
