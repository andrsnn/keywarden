"""Local approvers: this computer approves a key request, so you don't have to reply in the chat.

Built in: Touch ID on a Mac (touchid) and Windows Hello on Windows (hello). You can plug in your own program
instead: `keys touchid use command -- PROGRAM ARGS`. keywarden runs it with the reason as the last argument and
reads the last line it prints: "Verified" approves, "unavailable ..." means it can't ask right now, "Timeout"
means nobody answered, and anything else is a no. Run with --check, it prints "available" or "unavailable why".
A Python module can add an approver with register(name, approver).

Chat approvals use an approver only after you turn it on with `keys touchid on`. It is off by default.
Phone mode and protected keys already use the platform's approver alongside the phone.

An approver has:
  label                 what to call it in messages ("Touch ID")
  check()               "available", or "unavailable why"
  ask(reason, timeout)  "yes", "no", "timeout" or "unavailable"
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ.get("AGENT_KEYS_STATE") or Path.home() / ".agent-keys")
HERE = Path(__file__).resolve().parent
DEFAULT_TIMEOUT = 60  # seconds to wait for a tap before asking in the chat instead

POWERSHELL = (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                           "powershell.exe") if os.name == "nt" else "powershell")

WIN_HELLO = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$m = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($op, $t) { $task = $m.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
$V = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType=WindowsRuntime]
$a = Await ($V::CheckAvailabilityAsync()) ([Windows.Security.Credentials.UI.UserConsentVerifierAvailability])
if ("$a" -ne 'Available') { Write-Output "unavailable $a"; exit }
if ($env:AGENT_KEYS_CHECK_ONLY) { Write-Output 'available'; exit }
$r = Await ($V::RequestVerificationAsync($env:AGENT_KEYS_REASON)) ([Windows.Security.Credentials.UI.UserConsentVerificationResult])
Write-Output "$r"
"""


def config():
    try:
        return json.loads((STATE / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def result(text, code):
    """Turn an approver program's output into yes, no, timeout or unavailable."""
    lines = (text or "").strip().splitlines()
    last = lines[-1].strip() if lines else ""
    if last == "Verified" and code == 0:
        return "yes"
    if last == "Timeout":
        return "timeout"
    if not last or last.lower().startswith("unavailable") or code not in (0, None):
        return "unavailable"
    return "no"  # Canceled, RetriesExhausted, DeviceBusy...


class Program:
    """An approver that runs a program and reads the last line it prints."""
    label = "your approval program"

    def argv(self):
        return None

    def missing(self):
        return "unavailable: no approval program is set"

    def args(self, reason):
        return [reason]

    def env(self, reason, timeout, check=False):
        return dict(os.environ, AGENT_KEYS_REASON=reason, AGENT_KEYS_TIMEOUT=str(int(timeout)),
                    **({"AGENT_KEYS_CHECK_ONLY": "1"} if check else {}))

    def check_args(self):
        return ["--check"]

    def check(self):
        cmd = self.argv()
        if not cmd:
            return self.missing()
        try:
            r = subprocess.run(cmd + self.check_args(), capture_output=True, text=True, timeout=120,
                               env=self.env("", DEFAULT_TIMEOUT, check=True))
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"unavailable: {e}"
        out = (r.stdout.strip() or r.stderr.strip() or "unavailable").splitlines()
        return out[-1].strip() if out else "unavailable"

    def ask(self, reason, timeout=DEFAULT_TIMEOUT, procs=None):
        cmd = self.argv()
        if not cmd:
            return "unavailable"
        try:
            p = subprocess.Popen(cmd + self.args(reason), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 env=self.env(reason, timeout))
        except OSError:
            return "unavailable"
        if procs is not None:
            procs.append(p)  # so a caller can stop the prompt when the phone answers first
        try:
            out, _ = p.communicate(timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            return "timeout"
        return result(out, p.returncode)


class TouchID(Program):
    """Touch ID, falling back to the Mac's password. install.py compiles touchid.swift to ~/.agent-keys/touchid."""
    label = "Touch ID"

    def argv(self):
        if sys.platform != "darwin":
            return None
        b = STATE / "touchid"
        if b.exists():
            return [str(b)]
        return ["swift", str(HERE / "touchid.swift")] if shutil.which("swift") else None

    def missing(self):
        if sys.platform != "darwin":
            return "unavailable: Touch ID is only on a Mac"
        return "unavailable: install Xcode command line tools (xcode-select --install), then rerun install.py"


class WindowsHello(Program):
    label = "Windows Hello"

    def argv(self):
        if os.name != "nt":
            return None
        return [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", WIN_HELLO]

    def missing(self):
        return "unavailable: Windows Hello is only on Windows"

    def args(self, reason):
        return []

    def check_args(self):
        return []


class Command(Program):
    """Your own program, set with keys touchid use command -- PROGRAM ARGS."""
    label = "your approval program"

    def argv(self):
        cmd = config().get("local_approval_command")
        return list(cmd) if isinstance(cmd, list) and cmd else None


APPROVERS = {"touchid": TouchID(), "hello": WindowsHello(), "command": Command()}


def register(name, approver):
    """Add an approver. It needs label, check() and ask(reason, timeout, procs=None)."""
    APPROVERS[name] = approver


def platform_name():
    return "touchid" if sys.platform == "darwin" else "hello" if os.name == "nt" else None


def chosen():
    """The approver you turned on for chat approvals, or None while it's off (the default)."""
    name = config().get("local_approval")
    return name if name in APPROVERS else None


def enabled():
    """The approver chat approvals try first, or None."""
    name = chosen()
    return APPROVERS[name] if name else None


def for_taps():
    """The approver that answers alongside the phone in phone mode and for protected keys."""
    name = chosen() or platform_name()
    return APPROVERS.get(name) if name else None


def timeout():
    try:
        return max(5, float(config().get("local_approval_timeout", DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
