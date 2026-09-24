// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

import AppKit

/// The one window, and the only thing in this program a person sees.
///
/// It is deliberately plain. The step it exists for is the one the plan flags
/// as the place this flow can confuse somebody: **the installer looks finished
/// long before the helper is ready**, because ~80 MB of Python, numpy, bgsage
/// and weights are still arriving. So there is always a sentence saying what
/// is happening, and the progress bar never stops until something has actually
/// ended.
///
/// Every colour is a semantic `NSColor` and the card is an `NSBox` rather than
/// a layer-backed view, so dark mode costs nothing and a user who switches
/// appearance mid-install does not end up with black text on black.
final class InstallerWindow: NSWindow {

    private let headline = NSTextField(labelWithString: "GammonView Helper")
    private let stepTitle = NSTextField(labelWithString: "")
    private let detail = NSTextField(wrappingLabelWithString: "")
    private let noticeLabel = NSTextField(wrappingLabelWithString: "")
    private let progress = NSProgressIndicator()
    private let wordCard = NSBox()
    private let wordLabel = NSTextField(labelWithString: "")
    private let wordCaption = NSTextField(wrappingLabelWithString: "")
    private let primary = NSButton(title: "", target: nil, action: nil)
    private let alternate = NSButton(title: "", target: nil, action: nil)
    private let secondary = NSButton(title: "Quit", target: nil, action: nil)

    /// What the buttons do right now. Set by `render`, read by the actions.
    private var primaryAction: (() -> Void)?
    private var alternateAction: (() -> Void)?
    var onRetry: (() -> Void)?
    var onUpdate: (() -> Void)?
    var onUninstall: (() -> Void)?

    init() {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 400),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false)
        title = "GammonView Helper"
        isReleasedWhenClosed = false
        center()
        build()
    }

    private func build() {
        headline.font = .systemFont(ofSize: 20, weight: .semibold)
        stepTitle.font = .systemFont(ofSize: 14, weight: .semibold)

        detail.font = .systemFont(ofSize: 13)
        detail.textColor = .secondaryLabelColor
        // Pinned rather than free: a detail line that grows from one line to
        // three would move the progress bar and the buttons under the pointer.
        detail.setContentCompressionResistancePriority(.defaultLow, for: .vertical)

        noticeLabel.font = .systemFont(ofSize: 12)
        noticeLabel.textColor = .systemOrange
        noticeLabel.isHidden = true

        progress.style = .bar
        progress.isIndeterminate = true
        progress.controlSize = .regular

        wordLabel.font = .monospacedSystemFont(ofSize: 34, weight: .bold)
        wordLabel.alignment = .center
        // Selectable so it can be read by a screen reader and copied, but not
        // editable: it is a value to compare, never one to type.
        wordLabel.isSelectable = true

        wordCard.boxType = .custom
        wordCard.fillColor = .unemphasizedSelectedContentBackgroundColor
        wordCard.borderColor = .separatorColor
        wordCard.borderWidth = 1
        wordCard.cornerRadius = 10
        wordCard.titlePosition = .noTitle
        wordCard.contentView = wordLabel
        wordCard.isHidden = true

        wordCaption.font = .systemFont(ofSize: 13)
        wordCaption.textColor = .secondaryLabelColor
        wordCaption.alignment = .center
        wordCaption.isHidden = true

        primary.bezelStyle = .rounded
        primary.keyEquivalent = "\r"
        primary.target = self
        primary.action = #selector(primaryPressed)
        primary.isHidden = true

        // Never `keyEquivalent`: the destructive choice must not be what
        // Return does to a window somebody has not finished reading.
        alternate.bezelStyle = .rounded
        alternate.target = self
        alternate.action = #selector(alternatePressed)
        alternate.isHidden = true

        secondary.bezelStyle = .rounded
        secondary.target = self
        secondary.action = #selector(quitPressed)

        let buttons = NSStackView(views: [alternate, NSView(), secondary, primary])
        buttons.orientation = .horizontal
        buttons.spacing = 12

        let stack = NSStackView(views: [
            headline, stepTitle, detail, progress, noticeLabel,
            wordCard, wordCaption, NSView(), buttons,
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 28, left: 28, bottom: 24, right: 28)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView()
        content.addSubview(stack)
        contentView = content

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            progress.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -56),
            detail.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -56),
            detail.heightAnchor.constraint(greaterThanOrEqualToConstant: 48),
            noticeLabel.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -56),
            wordCard.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -56),
            wordCard.heightAnchor.constraint(equalToConstant: 74),
            wordCaption.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -56),
            buttons.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -56),
            wordLabel.centerXAnchor.constraint(equalTo: wordCard.centerXAnchor),
            wordLabel.centerYAnchor.constraint(equalTo: wordCard.centerYAnchor),
        ])
        progress.startAnimation(nil)
    }

    // MARK: - rendering

    func show(_ event: InstallerEvent) {
        switch event {
        case .step(let title, let text):
            stepTitle.stringValue = title
            detail.stringValue = text

        case .notice(let text):
            noticeLabel.stringValue = text
            noticeLabel.isHidden = false

        case .word(let word, let url):
            stepTitle.stringValue = "Link this computer to your account"
            detail.stringValue =
                "Your browser is opening gammonview.com. Sign in if it asks, "
                + "then choose this word:"
            wordLabel.stringValue = word
            wordCard.isHidden = false
            wordCaption.stringValue =
                "Three words will be offered. Only this one is right."
            wordCaption.isHidden = false
            setPrimary("Open the page again") { NSWorkspace.shared.open(url) }
            NSWorkspace.shared.open(url)

        case .choice(let message):
            progress.stopAnimation(nil)
            progress.isHidden = true
            stepTitle.stringValue = "Already installed"
            detail.stringValue = message
            // Update is the default because it is the common reason to run
            // this twice and because it is the one that undoes nothing.
            setPrimary("Update") { [weak self] in
                self?.beginWork("Getting ready")
                self?.onUpdate?()
            }
            setAlternate("Uninstall") { [weak self] in
                self?.beginWork("Removing")
                self?.onUninstall?()
            }

        case .finished(let title, let message):
            progress.stopAnimation(nil)
            progress.isHidden = true
            stepTitle.stringValue = title
            detail.stringValue = message
            wordCard.isHidden = true
            wordCaption.isHidden = true
            secondary.title = "Close"
            setPrimary("Done") { NSApp.terminate(nil) }

        case .failed(let message, let retry, let downloadPage):
            progress.stopAnimation(nil)
            progress.isHidden = true
            stepTitle.stringValue = "Something went wrong"
            detail.stringValue = message
            wordCard.isHidden = true
            wordCaption.isHidden = true
            if downloadPage {
                setPrimary("Open gammonview.com") {
                    NSWorkspace.shared.open(URL(string: "https://gammonview.com")!)
                }
            } else if retry {
                setPrimary("Try again") { [weak self] in self?.restart() }
            } else {
                primary.isHidden = true
            }
        }
    }

    private func setPrimary(_ title: String, _ action: @escaping () -> Void) {
        primary.title = title
        primary.isHidden = false
        primaryAction = action
    }

    private func setAlternate(_ title: String, _ action: @escaping () -> Void) {
        alternate.title = title
        alternate.isHidden = false
        alternateAction = action
    }

    /// Back to the working look: no buttons to press twice, bar moving again.
    ///
    /// Hiding the buttons is the part that matters. Every one of them starts
    /// something that takes minutes, and a second press during those minutes
    /// would run it concurrently with itself -- two `uv tool install`s into one
    /// directory, or an uninstall racing the update it was clicked next to.
    private func beginWork(_ title: String) {
        primary.isHidden = true
        alternate.isHidden = true
        noticeLabel.isHidden = true
        progress.isHidden = false
        progress.startAnimation(nil)
        stepTitle.stringValue = title
        detail.stringValue = ""
    }

    private func restart() {
        beginWork("Getting ready")
        onRetry?()
    }

    @objc private func primaryPressed() { primaryAction?() }
    @objc private func alternatePressed() { alternateAction?() }
    @objc private func quitPressed() { NSApp.terminate(nil) }
}
