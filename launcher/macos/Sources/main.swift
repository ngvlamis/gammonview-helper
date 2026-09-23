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

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()

        window = InstallerWindow()
        window.onRetry = { [weak self] in self?.start() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        window.show(.step(title: "Getting ready", detail: "Starting\u{2026}"))
        start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    private func start() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            Installer.run { event in self?.window.show(event) }
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
