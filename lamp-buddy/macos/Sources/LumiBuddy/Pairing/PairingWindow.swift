import AppKit

final class PairingWindowController: NSWindowController {
    private let hostField = NSTextField()
    private let codeField = NSTextField()
    private let statusLabel = NSTextField(labelWithString: " ")
    private let pairButton = NSButton(title: "Pair", target: nil, action: nil)
    private let cancelButton = NSButton(title: "Cancel", target: nil, action: nil)

    private let manager: PairingManager
    var onSuccess: ((PairingRecord) -> Void)?

    init(manager: PairingManager, initialHost: String? = nil) {
        self.manager = manager
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 240),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Pair with Lamp"
        window.isReleasedWhenClosed = false
        super.init(window: window)

        hostField.stringValue = initialHost ?? ""
        hostField.placeholderString = "lamp-xxxx.local"
        codeField.placeholderString = "6-digit code from your lamp's web UI"

        pairButton.target = self
        pairButton.action = #selector(submit)
        pairButton.keyEquivalent = "\r"

        cancelButton.target = self
        cancelButton.action = #selector(cancel)

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.lineBreakMode = .byWordWrapping
        statusLabel.maximumNumberOfLines = 2

        buildLayout()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) not used") }

    private func buildLayout() {
        guard let content = window?.contentView else { return }
        let hostLabel = NSTextField(labelWithString: "Lamp host")
        let codeLabel = NSTextField(labelWithString: "Pairing code")

        for v in [hostLabel, hostField, codeLabel, codeField, statusLabel, pairButton, cancelButton] {
            content.addSubview(v)
            v.translatesAutoresizingMaskIntoConstraints = false
        }

        NSLayoutConstraint.activate([
            hostLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            hostLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),

            hostField.topAnchor.constraint(equalTo: hostLabel.bottomAnchor, constant: 4),
            hostField.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            hostField.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),

            codeLabel.topAnchor.constraint(equalTo: hostField.bottomAnchor, constant: 14),
            codeLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),

            codeField.topAnchor.constraint(equalTo: codeLabel.bottomAnchor, constant: 4),
            codeField.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            codeField.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),

            statusLabel.topAnchor.constraint(equalTo: codeField.bottomAnchor, constant: 14),
            statusLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            statusLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),

            pairButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
            pairButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),

            cancelButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
            cancelButton.trailingAnchor.constraint(equalTo: pairButton.leadingAnchor, constant: -8),
        ])
    }

    @objc private func cancel() {
        window?.performClose(nil)
    }

    @objc private func submit() {
        let host = hostField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let code = codeField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty else {
            statusLabel.stringValue = "Host required."
            return
        }
        guard code.count == 6, code.allSatisfy({ $0.isNumber }) else {
            statusLabel.stringValue = "Enter the 6-digit code shown by your lamp's web UI."
            return
        }
        pairButton.isEnabled = false
        cancelButton.isEnabled = false
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.stringValue = "Pairing…"

        Task { @MainActor in
            do {
                let record = try await manager.pair(lampHost: host, code: code)
                statusLabel.textColor = .systemGreen
                statusLabel.stringValue = "Paired with \(record.lampHost)"
                onSuccess?(record)
                try? await Task.sleep(nanoseconds: 800_000_000)
                window?.performClose(nil)
            } catch {
                statusLabel.textColor = .systemRed
                let msg = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
                statusLabel.stringValue = "Failed: \(msg)"
                pairButton.isEnabled = true
                cancelButton.isEnabled = true
            }
        }
    }
}
