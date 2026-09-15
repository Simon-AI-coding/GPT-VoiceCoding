import AppKit

/// Lets the Duty Lamp show a hand while the shell is not the active app.
///
/// WindowServer ignores `NSCursor.set()` from an inactive app, and the lamp is a
/// non-activating panel of an accessory app, so without this the hand appears only
/// while the Control window has activated the shell. The only switch is the private
/// connection property `SetsCursorInBackground`. Its symbols are resolved at run time:
/// if a future macOS removes them, this is a no-op and the lamp keeps the arrow,
/// instead of the app failing to launch on a missing symbol.
enum BackgroundCursor {
    private typealias MainConnection = @convention(c) () -> Int32
    private typealias SetProperty =
        @convention(c) (Int32, Int32, CFString, CFTypeRef) -> Int32

    /// Returns whether WindowServer accepted the property.
    @discardableResult
    static func enable(
        resolve: (String) -> UnsafeMutableRawPointer? = { dlsym(dlopen(nil, RTLD_NOW), $0) }
    ) -> Bool {
        guard let main = resolve("CGSMainConnectionID"),
            let set = resolve("CGSSetConnectionProperty")
        else { return false }
        let connection = unsafeBitCast(main, to: MainConnection.self)()
        let status = unsafeBitCast(set, to: SetProperty.self)(
            connection, connection, "SetsCursorInBackground" as CFString, kCFBooleanTrue)
        return status == 0
    }
}
