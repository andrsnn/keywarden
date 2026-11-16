// Touch ID check for keywarden on macOS. install.py compiles it to ~/.agent-keys/touchid.
// Usage: touchid "reason"   prints Verified, Canceled or Timeout
//        touchid --check    prints available, or unavailable and why
// AGENT_KEYS_TIMEOUT=N closes the prompt after N seconds and prints Timeout, so the chat can ask instead.
import Foundation
import LocalAuthentication

let args = Array(CommandLine.arguments.dropFirst())
let ctx = LAContext()
var err: NSError?
// deviceOwnerAuthentication uses Touch ID and falls back to the Mac's password.
guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &err) else {
    print("unavailable \(err?.localizedDescription ?? "")")
    exit(0)
}
if args.first == "--check" {
    print("available")
    exit(0)
}
let reason = args.isEmpty ? "use an API key" : args.joined(separator: " ")
let done = DispatchSemaphore(value: 0)
var ok = false
var timedOut = false
if let s = ProcessInfo.processInfo.environment["AGENT_KEYS_TIMEOUT"], let secs = Double(s), secs > 0 {
    DispatchQueue.global().asyncAfter(deadline: .now() + secs) {
        timedOut = true
        ctx.invalidate()  // closes the prompt; evaluatePolicy then answers with an error
    }
}
ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, _ in
    ok = success
    done.signal()
}
done.wait()
print(ok ? "Verified" : (timedOut ? "Timeout" : "Canceled"))
