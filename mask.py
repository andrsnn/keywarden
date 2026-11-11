#!/usr/bin/env python3
"""Copy stdin to stdout, replacing every key value with <concealed by keywarden>.

While keys are approved, the Claude hook sends each command's output through this, so `curl -v`, `env` or a debug
print can't put a key in the chat. The key names come from AGENT_KEYS_NAMES, which `keys env` sets, and the
values from this process's own environment. It also hides each value's base64 form (as in Basic auth headers)."""
import base64
import os
import sys


def main():
    names = [n for n in os.environ.get("AGENT_KEYS_NAMES", "").split(",") if n]
    hide = set()
    for n in names:
        v = os.environ.get(n, "")
        if len(v) >= 6:
            hide.add(v.encode())
            hide.add(base64.b64encode(v.encode()).rstrip(b"="))
            for line in v.splitlines() if "\n" in v else []:  # a private key prints line by line
                if len(line.strip()) >= 16 and not line.startswith("-----"):
                    hide.add(line.strip().encode())
    hide = sorted(hide, key=len, reverse=True)
    src, dst = sys.stdin.buffer, sys.stdout.buffer
    while True:
        line = src.readline()
        if not line:
            break
        for h in hide:
            if h in line:
                line = line.replace(h, b"<concealed by keywarden>")
        dst.write(line)
        dst.flush()


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
