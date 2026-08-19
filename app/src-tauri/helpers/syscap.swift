// syscap — rendszerhang- (és opcionálisan képernyő-) rögzítő helper (ScreenCaptureKit).
// A Hangar bar meeting-felvétele indítja/állítja: a MÁSIK résztvevők hangját
// rögzíti (minden rendszerhang, a saját process hangja nélkül) WAV-ba, és
// --video módban a képernyőt is (H.264 .mov, rendszerhang + mikrofon hanggal).
//
// Használat:   syscap <kimenet.wav> [--video <kimenet.mov>]
// Leállítás:   SIGTERM/SIGINT VAGY stdin lezárása (a Rust ezt csinálja).
// Kimenet:     "READY" sor amikor a capture ténylegesen elindult,
//              "DONE <path>" sikeres lezáráskor.
// Kilépéskód:  0 = ok · 2 = nincs Screen Recording engedély / indítási hiba
//
// FONTOS (TCC): a Screen Recording engedély a SZÜLŐ apphoz (hangar.app)
// kötődik — a stabil codesign cert miatt build-ek közt megmarad.
//
// Fordítás: a src-tauri/build.rs végzi automatikusan (mtime-alapú újrafordítás).

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
        // A video frame-eket eldobjuk — csak azért van video output, mert nélküle
        // több macOS verzión az audio pipeline el sem indul.
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
                // WAV (RIFF, 16-bit int) fájl; a feldolgozó formátum a stream Float32-je.
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
        // A rögzítendő kijelző indexe (a bar képernyő-választója; 0 = első/fő).
        var displayIndex = 0
        if let idx = CommandLine.arguments.firstIndex(of: "--display"), idx + 1 < CommandLine.arguments.count {
            displayIndex = Int(CommandLine.arguments[idx + 1]) ?? 0
        }

        let writer = AudioWriter(url: url)
        let stream: SCStream
        do {
            // Ha nincs Screen Recording engedély, ez dob → exit 2, a Rust
            // mikrofon-only módra esik vissza és szól a usernek.
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            // A kiválasztott kijelző (a bar képernyő-választója); ha az index
            // kilóg, az elsőre esünk vissza.
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
            // KRITIKUS: a sampleRate/channelCount explicit kell — a default 0
            // némán kikapcsolja az audio-t. A writer az első bufferből veszi
            // át a tényleges formátumot, így eltérés sosem lehet.
            cfg.sampleRate = 48000
            cfg.channelCount = 2

            if let videoURL {
                // VIDEO MÓD: valódi felbontás (max 1920 széles), 20 fps, kurzorral.
                let scale = 2.0
                let pw = Double(display.width) * scale
                let ph = Double(display.height) * scale
                let downscale = min(1.0, 1920.0 / pw)
                cfg.width = Int(pw * downscale)
                cfg.height = Int(ph * downscale)
                cfg.minimumFrameInterval = CMTime(value: 1, timescale: 20)
                cfg.showsCursor = true
                cfg.queueDepth = 6
                // A mikrofont SZÁNDÉKOSAN nem vesszük a .mov-ba: a hangszóró-
                // áthallás visszhangot okozna. A dashboard a videó alá a
                // visszhang-szűrt kevert sávot (mic+system, ducking) játssza.
            } else {
                // AUDIO-ONLY MÓD: minimál video config — az audio pipeline
                // működéséhez kell egy élő video output is (SCK-sajátosság).
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

        // Leállítás-kezelés: SIGTERM/SIGINT + stdin EOF (a szülő lezárja a pipe-ot).
        let stopOnce = OnceFlag()
        let hasVideo = videoURL != nil
        let shutdown: @Sendable () -> Void = {
            guard stopOnce.take() else { return }
            Task {
                try? await stream.stopCapture()
                writer.close()
                // Video módban a .mov moov-atom finalizálásának időt kell hagyni,
                // különben csonka (rossz duration-ű) fájl marad.
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
            // Blokkoló olvasás: ha a szülő lezárja az stdint (vagy meghal), EOF jön.
            let data = FileHandle.standardInput.readDataToEndOfFile()
            _ = data
            shutdown()
        }

        // Életben tartás.
        while true {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }
}

/// SCRecordingOutput delegate — hibák jelzése (a finalize a stopCapture-nél történik).
@available(macOS 15.0, *)
final class RecordingDelegate: NSObject, SCRecordingOutputDelegate {
    static let shared = RecordingDelegate()
    func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        FileHandle.standardError.write("ERR recording: \(error)\n".data(using: .utf8)!)
    }
}

/// Egyszeri futtatás garantálása (signal + stdin EOF versenyhelyzetére).
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
