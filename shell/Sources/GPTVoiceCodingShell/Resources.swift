import Foundation

extension Bundle {
    /// SwiftPM uses a sibling bundle in a checkout; the app pipeline places it
    /// in Contents/Resources and records its name with the bundle's identity.
    static let shell: Bundle = {
        if let name = Bundle.main.object(forInfoDictionaryKey: "ShellResourceBundle") as? String {
            return Bundle(url: Bundle.main.resourceURL!.appendingPathComponent(name))!
        }
        return .module
    }()
}
