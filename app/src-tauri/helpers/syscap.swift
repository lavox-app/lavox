// syscap — system-audio (and optionally screen) capture helper (ScreenCaptureKit).
// Started/stopped by the Hangar bar's meeting recording: it records the OTHER
// participants' audio (all system audio, excluding this process's own output)
// to WAV, and in --video mode the screen as well (H.264 .mov, with system
// audio + microphone).
//
// Usage:       syscap <out.wav> [--video <out.mov>]
// Shutdown:    SIGTERM/SIGINT OR closing stdin (that's what the Rust side does).
// Output:      a "READY" line once capture has actually started,
//              "DONE <path>" on successful finalization.
// Exit code:   0 = ok · 2 = no Screen Recording permission / startup error
//
// IMPORTANT (TCC): the Screen Recording permission is bound to the PARENT app
// (hangar.app) — thanks to the stable codesign cert it survives across builds.
//
// Compilation: handled automatically by src-tauri/build.rs (mtime-based rebuild).

import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

final class AudioWriter: NSObject, SCStreamOutput, SCStreamDelegate {
    let url: URL
    private var file: AVAudioFile?
    private let lock = NSLock()

    init(url: URL) { self.url = url }

    private var videoCount = 0
    private var audioCount = 0

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        // Video frames are dropped — the video output only exists because on
        // several macOS versions the audio pipeline won't even start without it.
        if type == .screen {
            videoCount += 1
            if videoCount == 1 || videoCount % 30 == 0 {
                FileHandle.standardError.write("DBG video#\(videoCount)\n".data(using: .utf8)!)
            }
            return
        }
        audioCount += 1
        if audioCount == 1 {
            FileHandle.standardError.write("DBG first audio buffer, samples=\(sb.numSamples)\n".data(using: .utf8)!)
        }
        guard type == .audio, sb.isValid, sb.numSamples > 0 else { return }
        lock.lock()
        defer { lock.unlock() }
        do {
            guard let fmtDesc = sb.formatDescription else { return }
            let fmt = AVAudioFormat(cmAudioFormatDescription: fmtDesc)
            if file == nil {
                // WAV (RIFF, 16-bit int) file; the processing format is the stream's Float32.
                let settings: [String: Any] = [
                    AVFormatIDKey: kAudioFormatLinearPCM,
                    AVSampleRateKey: fmt.sampleRate,
                    AVNumberOfChannelsKey: fmt.channelCount,
                    AVLinearPCMBitDepthKey: 16,
                    AVLinearPCMIsFloatKey: false,
                    AVLinearPCMIsBigEndianKey: false,
                ]
                file = try AVAudioFile(
                    forWriting: url,
                    settings: settings,
                    commonFormat: fmt.commonFormat,
                    interleaved: fmt.isInterleaved
                )
                print("READY")
                fflush(stdout)
            }
            guard let file else { return }
            let frames = AVAudioFrameCount(sb.numSamples)
            guard let pcm = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: frames) else { return }
            pcm.frameLength = frames
            try sb.copyPCMData(fromRange: 0..<Int(frames), into: pcm.mutableAudioBufferList)
            try file.write(from: pcm)
        } catch {
            FileHandle.standardError.write("ERR write: \(error)\n".data(using: .utf8)!)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("ERR stream stopped: \(error)\n".data(using: .utf8)!)
        exit(2)
    }

    func close() {
        lock.lock()
        file = nil // AVAudioFile dealloc = finalize header
        lock.unlock()
    }
}

@main
struct SysCap {
    static func main() async {
        guard CommandLine.arguments.count >= 2 else {
            FileHandle.standardError.write("usage: syscap <out.wav> [--video <out.mov>]\n".data(using: .utf8)!)
            exit(1)
        }
        let url = URL(fileURLWithPath: CommandLine.arguments[1])
        var videoURL: URL? = nil
        if let idx = CommandLine.arguments.firstIndex(of: "--video"), idx + 1 < CommandLine.arguments.count {
            videoURL = URL(fileURLWithPath: CommandLine.arguments[idx + 1])
        }
        // Index of the display to capture (the bar's screen picker; 0 = first/main).
        var displayIndex = 0
        if let idx = CommandLine.arguments.firstIndex(of: "--display"), idx + 1 < CommandLine.arguments.count {
            displayIndex = Int(CommandLine.arguments[idx + 1]) ?? 0
        }

        let writer = AudioWriter(url: url)
        let stream: SCStream
        do {
            // Without the Screen Recording permission this throws → exit 2; the
            // Rust side falls back to microphone-only mode and notifies the user.
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            // The selected display (the bar's screen picker); if the index is
            // out of range, fall back to the first one.
            let display: SCDisplay
            if displayIndex >= 0 && displayIndex < content.displays.count {
                display = content.displays[displayIndex]
            } else if let first = content.displays.first {
                display = first
            } else {
                FileHandle.standardError.write("NO_DISPLAY\n".data(using: .utf8)!)
                exit(2)
            }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let cfg = SCStreamConfiguration()
            cfg.capturesAudio = true
            cfg.excludesCurrentProcessAudio = true
            // CRITICAL: sampleRate/channelCount must be explicit — the default 0
            // silently disables audio. The writer adopts the actual format from
            // the first buffer, so a mismatch is impossible.
            cfg.sampleRate = 48000
            cfg.channelCount = 2

            if let videoURL {
                // VIDEO MODE: real resolution (max 1920 wide), 20 fps, with cursor.
                let scale = 2.0
                let pw = Double(display.width) * scale
                let ph = Double(display.height) * scale
                let downscale = min(1.0, 1920.0 / pw)
                cfg.width = Int(pw * downscale)
                cfg.height = Int(ph * downscale)
                cfg.minimumFrameInterval = CMTime(value: 1, timescale: 20)
                cfg.showsCursor = true
                cfg.queueDepth = 6
                // The microphone is INTENTIONALLY kept out of the .mov: speaker
                // bleed-through would cause echo. The dashboard plays the
                // echo-filtered mixed track (mic+system, ducking) under the video.
            } else {
                // AUDIO-ONLY MODE: minimal video config — the audio pipeline
                // needs a live video output to work (an SCK quirk).
                cfg.width = 64
                cfg.height = 36
                cfg.minimumFrameInterval = CMTime(value: 1, timescale: 30)
                cfg.queueDepth = 5
            }

            stream = SCStream(filter: filter, configuration: cfg, delegate: writer)
            try stream.addStreamOutput(writer, type: .audio, sampleHandlerQueue: DispatchQueue(label: "syscap.audio"))
            try stream.addStreamOutput(writer, type: .screen, sampleHandlerQueue: DispatchQueue(label: "syscap.video"))

            if let videoURL {
                guard #available(macOS 15.0, *) else {
                    FileHandle.standardError.write("VIDEO_NEEDS_MACOS15\n".data(using: .utf8)!)
                    exit(2)
                }
                let recCfg = SCRecordingOutputConfiguration()
                recCfg.outputURL = videoURL
                recCfg.outputFileType = .mov
                recCfg.videoCodecType = .h264
                let recOut = SCRecordingOutput(configuration: recCfg, delegate: RecordingDelegate.shared)
                try stream.addRecordingOutput(recOut)
            }

            try await stream.startCapture()
        } catch {
            FileHandle.standardError.write("TCC_OR_START_ERR: \(error)\n".data(using: .utf8)!)
            exit(2)
        }

        // Shutdown handling: SIGTERM/SIGINT + stdin EOF (the parent closes the pipe).
        let stopOnce = OnceFlag()
        let hasVideo = videoURL != nil
        let shutdown: @Sendable () -> Void = {
            guard stopOnce.take() else { return }
            Task {
                try? await stream.stopCapture()
                writer.close()
                // In video mode the .mov moov-atom finalization needs some time,
                // otherwise a truncated file (with a wrong duration) is left behind.
                if hasVideo {
                    try? await Task.sleep(nanoseconds: 1_200_000_000)
                }
                print("DONE \(url.path)")
                fflush(stdout)
                exit(0)
            }
        }

        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: DispatchQueue.global())
        sigTerm.setEventHandler(handler: shutdown)
        sigTerm.resume()
        let sigInt = DispatchSource.makeSignalSource(signal: SIGINT, queue: DispatchQueue.global())
        sigInt.setEventHandler(handler: shutdown)
        sigInt.resume()

        DispatchQueue.global().async {
            // Blocking read: if the parent closes stdin (or dies), EOF arrives.
            let data = FileHandle.standardInput.readDataToEndOfFile()
            _ = data
            shutdown()
        }

        // Keep-alive.
        while true {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }
}

/// SCRecordingOutput delegate — error reporting (finalization happens at stopCapture).
@available(macOS 15.0, *)
final class RecordingDelegate: NSObject, SCRecordingOutputDelegate {
    static let shared = RecordingDelegate()
    func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        FileHandle.standardError.write("ERR recording: \(error)\n".data(using: .utf8)!)
    }
}

/// Guarantees single execution (for the signal + stdin EOF race).
final class OnceFlag: @unchecked Sendable {
    private var done = false
    private let lock = NSLock()
    func take() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if done { return false }
        done = true
        return true
    }
}
