"""Run the test suites in order, keeping phone-test state out of the user's settings."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    failed = []
    with tempfile.TemporaryDirectory(prefix="keywarden-tests-") as tmp:
        env = dict(os.environ, AGENT_KEYS_STATE=tmp, AGENT_KEYS_NS="keywarden-suite-test")
        suites = [[sys.executable, str(HERE / name)] for name in (
            "test_hook.py", "test_prompt.py", "test_chat.py", "test_backends.py", "test_phone.py",
            "test_integrations.py", "test_scrub.py", "test_native.py")]
        if shutil.which("bash"):
            suites.append(["bash", str(HERE / "test_mask.sh")])
        else:
            print("SKIP test_mask.sh: Bash is not installed", flush=True)
        for cmd in suites:
            name = Path(cmd[1]).name
            print(f"\nRunning {name}", flush=True)
            try:
                code = subprocess.run(cmd, env=env, cwd=HERE, timeout=180).returncode
            except subprocess.TimeoutExpired:
                print(f"TIMEOUT {name}", flush=True)
                code = 1
            if code:
                failed.append(name)
    print(f"\n{len(suites) - len(failed)}/{len(suites)} suites passed.", flush=True)
    if failed:
        print("Failed: " + ", ".join(failed))
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
