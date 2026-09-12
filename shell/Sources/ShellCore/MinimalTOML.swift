import Foundation

/// One named string out of one named table, and deliberately no more.
///
/// The shell asks two questions of the engine's configuration: `[engine]
/// socket_path`, because the socket path is not derivable from the state path,
/// and `[adapters.settings.companion_channel] token_env`, because the credential
/// file must not duplicate a variable name owned by that configuration. The
/// engine reads the file properly with `tomllib`; a scanner that answers only a
/// caller's named string cannot grow into a second configuration reader.
///
/// It understands what a string assignment looks like: table headers, `#`
/// comments, basic and literal strings. Anything else it reports as unreadable
/// rather than guessing, because a misread here would hand the shell the wrong
/// socket and it would report an engine missing that was running all along.
enum MinimalTOML {
    /// Edit only the named assignment. Parsing and validation remain in Python.
    static func setting(
        _ value: JSONValue, forKey key: String, inTable table: String, of text: String
    ) throws -> String {
        let rendered = try literal(value)
        var lines = text.components(separatedBy: "\n")
        let starts = statementStarts(in: lines)
        var currentTable = ""
        var insertion: Int?
        for index in lines.indices {
            guard starts.contains(index) else { continue }
            let line = lines[index]
            if let name = tableName(line) {
                if currentTable == table { break }
                currentTable = name
                if name == table { insertion = index + 1 }
                continue
            }
            guard currentTable == table,
                let equal = line.firstIndex(of: "="),
                line[..<equal].trimmingCharacters(in: .whitespaces) == key
            else { continue }
            let suffix = line[line.index(after: equal)...]
            let comment = commentStart(in: suffix) ?? line.endIndex
            let rawValue = line[line.index(after: equal)..<comment]
            let start = rawValue.firstIndex { !$0.isWhitespace } ?? rawValue.startIndex
            let end = rawValue.lastIndex { !$0.isWhitespace }.map { line.index(after: $0) } ?? start
            lines[index].replaceSubrange(start..<end, with: rendered)
            return lines.joined(separator: "\n")
        }
        let assignment = "\(key) = \(rendered)"
        if let insertion {
            lines.insert(assignment, at: insertion)
            return lines.joined(separator: "\n")
        }
        let separator = text.isEmpty || text.hasSuffix("\n") ? "" : "\n"
        return text + separator + "[\(table)]\n" + assignment + "\n"
    }

    private static func literal(_ value: JSONValue) throws -> String {
        switch value {
        case .string, .number:
            let data = try JSONSerialization.data(
                withJSONObject: value.raw, options: [.fragmentsAllowed, .withoutEscapingSlashes])
            return String(decoding: data, as: UTF8.self)
        default:
            throw ConfigurationFailure.unreadable("a setting must be a string or a number")
        }
    }

    static func removing(table: String, from text: String) -> String {
        var removing = false
        let lines = text.components(separatedBy: "\n")
        let starts = statementStarts(in: lines)
        return lines.enumerated().compactMap { index, line in
            if starts.contains(index), let name = tableName(line) {
                removing = name == table || name.hasPrefix(table + ".")
            }
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if starts.contains(index), trimmed.isEmpty || trimmed.hasPrefix("#") { return line }
            if index == lines.count - 1, line.isEmpty { return line }
            return removing ? nil : line
        }.joined(separator: "\n")
    }

    static func removing(key: String, inTable table: String, from text: String) -> String {
        let lines = text.components(separatedBy: "\n")
        let starts = statementStarts(in: lines)
        var currentTable = ""
        return lines.enumerated().compactMap { index, line in
            guard starts.contains(index) else { return line }
            if let name = tableName(line) {
                currentTable = name
                return line
            }
            guard currentTable == table, let equal = line.firstIndex(of: "="),
                line[..<equal].trimmingCharacters(in: .whitespaces) == key
            else { return line }
            if let comment = commentStart(in: line[line.index(after: equal)...]) {
                return String(line[comment...])
            }
            return nil
        }.joined(separator: "\n")
    }

    /// Locate statements, not values: quoted text and multiline arrays may
    /// contain lines that look like tables. They remain opaque to the editor.
    private static func statementStarts(in lines: [String]) -> Set<Int> {
        var starts = Set<Int>()
        var quote: Character?
        var multiline = false
        var brackets = 0
        for (number, line) in lines.enumerated() {
            if quote == nil && brackets == 0 { starts.insert(number) }
            let characters = Array(line)
            var index = 0
            while index < characters.count {
                let character = characters[index]
                if let closing = quote {
                    if closing == "\"", character == "\\" {
                        index += 2
                        continue
                    }
                    if character == closing {
                        var end = index + 1
                        while end < characters.count && characters[end] == closing { end += 1 }
                        if !multiline || end - index >= 3 {
                            quote = nil
                            index = multiline ? end : index + 1
                            multiline = false
                            continue
                        }
                    }
                } else if character == "#" {
                    break
                } else if character == "\"" || character == "'" {
                    quote = character
                    multiline =
                        index + 2 < characters.count
                        && characters[index + 1] == character && characters[index + 2] == character
                    index += multiline ? 3 : 1
                    continue
                } else if character == "[" || character == "{" {
                    brackets += 1
                } else if character == "]" || character == "}" {
                    brackets -= 1
                }
                index += 1
            }
        }
        return starts
    }

    private static func tableName(_ line: String) -> String? {
        let line = line.trimmingCharacters(in: .whitespaces)
        guard line.hasPrefix("["), let end = line.firstIndex(of: "]") else { return nil }
        return String(line[line.index(after: line.startIndex)..<end])
            .trimmingCharacters(in: .whitespaces)
    }

    private static func commentStart(in text: Substring) -> String.Index? {
        var quote: Character?
        var escaped = false
        for index in text.indices {
            let character = text[index]
            if escaped {
                escaped = false
                continue
            }
            if quote == "\"", character == "\\" {
                escaped = true
                continue
            }
            if let closing = quote {
                if character == closing { quote = nil }
            } else if character == "\"" || character == "'" {
                quote = character
            } else if character == "#" {
                return index
            }
        }
        return nil
    }

    /// The raw text of `key` in `table`, or nil when the table or key is absent.
    static func string(forKey key: String, inTable table: String, of text: String) throws
        -> String?
    {
        var currentTable = ""
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            if line.hasPrefix("[") {
                guard let end = line.firstIndex(of: "]") else { continue }
                currentTable = String(line[line.index(after: line.startIndex)..<end])
                    .trimmingCharacters(in: .whitespaces)
                continue
            }
            guard currentTable == table, let separator = line.firstIndex(of: "=") else { continue }
            let name = line[line.startIndex..<separator].trimmingCharacters(in: .whitespaces)
            guard name == key else { continue }
            let value = line[line.index(after: separator)...].trimmingCharacters(in: .whitespaces)
            return try unquote(value, key: key, table: table)
        }
        return nil
    }

    private static func unquote(_ value: String, key: String, table: String) throws -> String {
        // A literal string is taken as written; a basic string accepts TOML's
        // own escapes so the shell and engine cannot read different values.
        if value.hasPrefix("'") {
            guard let end = value.dropFirst().firstIndex(of: "'") else {
                throw ConfigurationFailure.unreadable("[\(table)] \(key) is not a closed string")
            }
            return String(value[value.index(after: value.startIndex)..<end])
        }
        guard value.hasPrefix("\"") else {
            throw ConfigurationFailure.unreadable("[\(table)] \(key) must be a quoted string")
        }
        var unescaped = ""
        var index = value.index(after: value.startIndex)
        while index < value.endIndex {
            let character = value[index]
            if character == "\"" { return unescaped }
            if character == "\\" {
                index = value.index(after: index)
                guard index < value.endIndex else { break }
                switch value[index] {
                case "n": unescaped.append("\n")
                case "t": unescaped.append("\t")
                case "r": unescaped.append("\r")
                case "b": unescaped.append("\u{08}")
                case "f": unescaped.append("\u{0C}")
                case "\\": unescaped.append("\\")
                case "\"": unescaped.append("\"")
                case "u", "U":
                    // TOML's own escapes, and `tomllib` reads them. Refusing one
                    // the engine accepts would make the shell act on a different
                    // socket path or credential-variable name.
                    let digits = value[index] == "u" ? 4 : 8
                    let (scalar, next) = try Self.scalar(
                        in: value, after: index, digits: digits, key: key, table: table)
                    unescaped.append(Character(scalar))
                    index = next
                default:
                    throw ConfigurationFailure.unreadable(
                        "[\(table)] \(key) uses an escape this shell does not read")
                }
            } else {
                unescaped.append(character)
            }
            index = value.index(after: index)
        }
        throw ConfigurationFailure.unreadable("[\(table)] \(key) is not a closed string")
    }

    /// One `\u`/`\U` escape, and where reading resumes after it.
    private static func scalar(
        in value: String, after backslash: String.Index, digits: Int, key: String, table: String
    ) throws -> (Unicode.Scalar, String.Index) {
        let start = value.index(after: backslash)
        guard let end = value.index(start, offsetBy: digits, limitedBy: value.endIndex),
            let code = UInt32(value[start..<end], radix: 16),
            let scalar = Unicode.Scalar(code)
        else {
            throw ConfigurationFailure.unreadable(
                "[\(table)] \(key) has an escape that is not \(digits) hexadecimal digits")
        }
        return (scalar, value.index(before: end))
    }
}

public enum ConfigurationFailure: Error, Equatable, Sendable {
    case unreadable(String)

    public var detail: String {
        switch self {
        case .unreadable(let detail): return detail
        }
    }
}
