import Testing

@testable import ShellCore

@Suite struct MinimalTOMLEditorTests {
    @Test func removingTelegramKeepsSurroundingCommentsAndBlankLines() {
        let original =
            "# expert file\n[adapters.settings.companion_channel]\nchat_id = '42'\n\n# how long a call may go quiet\n[policy]\nsilence_end_seconds = 60\n"
        #expect(
            MinimalTOML.removing(table: "adapters.settings.companion_channel", from: original)
                == "# expert file\n\n# how long a call may go quiet\n[policy]\nsilence_end_seconds = 60\n"
        )
    }
    @Test func changingOneValueKeepsCommentsAndUnknownKeys() throws {
        let original =
            "# my choices\n[delegate] # agent\nmodel  = 'old'  # cost\neffort = 'low'\n\n[expert]\nanswer = 42\n"
        let expected =
            "# my choices\n[delegate] # agent\nmodel  = \"new\"  # cost\neffort = 'low'\n\n[expert]\nanswer = 42\n"
        #expect(
            try MinimalTOML.setting(
                .string("new"), forKey: "model", inTable: "delegate", of: original) == expected)
    }

    @Test func bindingCanAddAndRemoveItsTableWithoutRewritingOtherTables() throws {
        let original = "[adapters]\ncompanion_channel = 'null'\n[expert]\nkeep = 'yes'\n"
        let selected = try MinimalTOML.setting(
            .string("telegram"), forKey: "companion_channel", inTable: "adapters", of: original)
        let bound = try MinimalTOML.setting(
            .string("42"), forKey: "chat_id", inTable: "adapters.settings.companion_channel",
            of: selected)
        #expect(
            bound
                == "[adapters]\ncompanion_channel = \"telegram\"\n[expert]\nkeep = 'yes'\n[adapters.settings.companion_channel]\nchat_id = \"42\"\n"
        )
        #expect(
            MinimalTOML.removing(table: "adapters.settings.companion_channel", from: bound)
                == selected)
    }

    @Test func unknownMultilineTextIsNotMistakenForAnEditableTable() throws {
        let original =
            "[expert]\nnotes = '''\n[delegate]\nmodel = 'a quotation'\n'''\n[delegate]\nmodel = 'old'\n"
        let expected =
            "[expert]\nnotes = '''\n[delegate]\nmodel = 'a quotation'\n'''\n[delegate]\nmodel = \"new\"\n"
        #expect(
            try MinimalTOML.setting(
                .string("new"), forKey: "model", inTable: "delegate", of: original) == expected)
    }
}
