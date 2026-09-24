// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

//
// The GammonView installer for macOS.
//
// It finds `uv`, installs `gammonview-helper` from PyPI into a private
// directory, registers a login item, and hands pairing to the package it just
// installed. Then it stops. That list is the entire specification, and the
// shortness of it is the design: this program ships **unsigned**, so a person
// gets past Gatekeeper exactly once, and anything that changes must therefore
// live on the other side of PyPI where updating it costs nobody a dialog.
//
// See `docs/DesktopHelper.md` in the GammonView repository, "The native
// surface is a launcher, and it is frozen".
//

import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: InstallerWindow!

    /// Which sequence Try Again would run.
    ///
    /// Held rather than recomputed, because by the time something has failed
    /// the machine no longer answers the question `Installer.start` asks: a
    /// half-finished install has the helper on disk, so re-deciding would show
    /// the "already installed" screen to somebody whose install just broke,
    /// and offer to uninstall it as the fix.
    private var action: ((@escaping (InstallerEvent) -> Void) -> Void) = Installer.start

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()

        window = InstallerWindow()
        window.onRetry = { [weak self] in self?.run() }
        window.onUpdate = { [weak self] in self?.run(Installer.install) }
        window.onUninstall = { [weak self] in self?.run(Installer.uninstall) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        window.show(.step(title: "Getting ready", detail: "Starting\u{2026}"))
        run()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    /// Run a sequence on a background queue, remembering it for Try Again.
    private func run(
        _ next: ((@escaping (InstallerEvent) -> Void) -> Void)? = nil
    ) {
        if let next { action = next }
        let sequence = action
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            sequence { event in self?.window.show(event) }
        }
    }

    /// The smallest menu that still behaves like a Mac application.
    ///
    /// Built in code because there is no nib: without it Command-Q does
    /// nothing, and an installer somebody cannot quit with the shortcut every
    /// other app has is an installer they will force-quit.
    private func buildMenu() {
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "Hide GammonView Installer",
            action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Quit GammonView Installer",
            action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let appItem = NSMenuItem()
        appItem.submenu = appMenu

        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(
            withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let editItem = NSMenuItem()
        editItem.submenu = editMenu

        let bar = NSMenu()
        bar.addItem(appItem)
        bar.addItem(editItem)
        NSApp.mainMenu = bar
    }
}

let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = AppDelegate()
application.delegate = delegate
application.run()
