// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

import Foundation

enum ShellError: LocalizedError {
    case couldNotStart(String, Error)
    case failed(String, Int32, String)

    var errorDescription: String? {
        switch self {
        case .couldNotStart(let what, let why):
            return "Could not start \(what): \(why.localizedDescription)"
        case .failed(let what, let code, let tail):
            return tail.isEmpty ? "\(what) failed (exit \(code))." : "\(what) failed.\n\n\(tail)"
        }
    }
}

/// Complete lines out of a pipe that delivers arbitrary chunks.
///
/// Both things this installer reads are line-oriented and neither tolerates a
/// split: half a JSON object is not a pairing event, and half a progress line
/// is a status label showing nonsense. `\r` ends a line as well as `\n`
/// because `uv` redraws its progress bar by returning to the start of one.
final class LineBuffer {
    private var pending: [UInt8] = []
    private let emit: (String) -> Void

    init(_ emit: @escaping (String) -> Void) { self.emit = emit }

    func feed(_ data: Data) {
        pending.append(contentsOf: data)
        while let end = pending.firstIndex(where: { $0 == 0x0A || $0 == 0x0D }) {
            let line = Array(pending[..<end])
            pending.removeFirst(end + 1)
            deliver(line)
        }
    }

    /// Whatever arrived without a newline after it -- which is how a process
    /// that dies mid-sentence says the most useful thing it will ever say.
    func flush() {
        deliver(pending)
        pending.removeAll()
    }

    private func deliver(_ bytes: [UInt8]) {
        guard let text = String(bytes: bytes, encoding: .utf8) else { return }
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        if !trimmed.isEmpty { emit(trimmed) }
    }
}

/// Strip the escape sequences `uv` colours and repositions its output with.
///
/// Shown in a label rather than a terminal, so `ESC[2K` is four characters of
/// noise rather than an instruction.
func plain(_ line: String) -> String {
    var out = ""
    var inEscape = false
    for character in line {
        if inEscape {
            // CSI runs until a letter; that is enough for everything uv emits.
            if character.isLetter { inEscape = false }
        } else if character == "\u{1B}" {
            inEscape = true
        } else {
            out.append(character)
        }
    }
    return out.trimmingCharacters(in: .whitespaces)
}

/// Run something to completion, streaming both its outputs.
///
/// Synchronous on purpose: the installer is a sequence of steps that genuinely
/// depend on each other, and it runs on a background queue with the UI updated
/// through callbacks. Making the steps async would buy concurrency there is
/// nothing to overlap with.
@discardableResult
func runProcess(
    _ executable: URL,
    _ arguments: [String],
    what: String,
    environment extra: [String: String] = [:],
    onOutput: ((String) -> Void)? = nil,
    onError: ((String) -> Void)? = nil
) throws -> Int32 {
    let task = Process()
    task.executableURL = executable
    task.arguments = arguments
    if !extra.isEmpty {
        var env = ProcessInfo.processInfo.environment
        for (key, value) in extra { env[key] = value }
        task.environment = env
    }

    let outPipe = Pipe(), errPipe = Pipe()
    task.standardOutput = outPipe
    task.standardError = errPipe
    task.standardInput = FileHandle.nullDevice

    // The last few lines of stderr, kept so that a failure can say what the
    // program said rather than only that it exited non-zero.
    var tail: [String] = []
    let tailLock = NSLock()

    let outBuffer = LineBuffer { line in onOutput?(line) }
    let errBuffer = LineBuffer { line in
        tailLock.lock()
        tail.append(line)
        if tail.count > 6 { tail.removeFirst() }
        tailLock.unlock()
        onError?(line)
    }

    outPipe.fileHandleForReading.readabilityHandler = { outBuffer.feed($0.availableData) }
    errPipe.fileHandleForReading.readabilityHandler = { errBuffer.feed($0.availableData) }

    do { try task.run() } catch { throw ShellError.couldNotStart(what, error) }
    task.waitUntilExit()

    // Draining after exit rather than trusting the handlers: the last write of
    // a short-lived process routinely lands after `waitUntilExit` returns, and
    // that write is usually the error message.
    outPipe.fileHandleForReading.readabilityHandler = nil
    errPipe.fileHandleForReading.readabilityHandler = nil
    outBuffer.feed(outPipe.fileHandleForReading.readDataToEndOfFile())
    errBuffer.feed(errPipe.fileHandleForReading.readDataToEndOfFile())
    outBuffer.flush()
    errBuffer.flush()

    if task.terminationStatus != 0 {
        tailLock.lock()
        let message = tail.map(plain).filter { !$0.isEmpty }.joined(separator: "\n")
        tailLock.unlock()
        throw ShellError.failed(what, task.terminationStatus, message)
    }
    return task.terminationStatus
}
