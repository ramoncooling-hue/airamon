import AppKit
import Vision
import PDFKit

// A forcibly terminated backend cannot reap children. Stop this local helper
// when its parent disappears as well as on the normal worker cancellation path.
let backendPID = getppid()
let parentWatch = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
parentWatch.schedule(deadline: .now() + 1, repeating: 1)
parentWatch.setEventHandler {
    if getppid() != backendPID { exit(130) }
}
parentWatch.resume()

func recognize(_ image: CGImage) throws -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    request.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: image).perform([request])
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

do {
    guard CommandLine.arguments.count == 3 else {
        throw NSError(domain: "MinDockOCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "Usage: mindock-ocr --image|--pdf path"])
    }
    let path = CommandLine.arguments[2]
    if CommandLine.arguments[1] == "--pdf" {
        guard let document = PDFDocument(url: URL(fileURLWithPath: path)) else { throw NSError(domain: "MinDockOCR", code: 2) }
        var pages: [[String: Any]] = []
        for index in 0..<document.pageCount {
            let text: String = try autoreleasepool {
                guard let page = document.page(at: index) else { return "" }
                let bounds = page.bounds(for: .mediaBox)
                let scale = min(2.5, 2200 / max(bounds.width, bounds.height))
                let size = NSSize(width: bounds.width * scale, height: bounds.height * scale)
                let thumbnail = page.thumbnail(of: size, for: .mediaBox)
                var rect = NSRect(origin: .zero, size: size)
                guard let cgImage = thumbnail.cgImage(forProposedRect: &rect, context: nil, hints: nil) else { return "" }
                return try recognize(cgImage)
            }
            if !text.isEmpty { pages.append(["page": index + 1, "text": text]) }
            let progress = try JSONSerialization.data(withJSONObject: [
                "event": "progress", "page": index + 1, "total": document.pageCount
            ])
            FileHandle.standardError.write(progress)
            FileHandle.standardError.write(Data("\n".utf8))
        }
        let data = try JSONSerialization.data(withJSONObject: pages)
        FileHandle.standardOutput.write(data)
    } else {
        guard let image = NSImage(contentsOfFile: path),
              let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { throw NSError(domain: "MinDockOCR", code: 3) }
        print(try recognize(cgImage))
    }
} catch {
    FileHandle.standardError.write(Data(error.localizedDescription.utf8))
    exit(1)
}
