#!/usr/bin/env bash
# Run: bash test_mask.sh   Checks the output filter the hook adds while keys are approved.
# A fake key is set in this script only. Prints PASS or FAIL lines, never the fake value.
PY="$(python -c 'import sys; print(sys.executable.replace(chr(92), "/"))')"
MASK="\"$PY\" -u \"$(cd "$(dirname "$0")" && pwd)/mask.py\""
export FAKE_SECRET="abcDEF123456ghiJKL"
export AGENT_KEYS_NAMES=FAKE_SECRET
out=$(eval "{ cd /; pwd; printf 'Authorization: Bearer %s\n' \"\$FAKE_SECRET\"; printf 'basic %s\n' \"\$(printf %s \"\$FAKE_SECRET\" | base64)\"; echo stderr-line >&2; (exit 3)
} > >($MASK) 2> >($MASK >&2)" 2>/tmp/ak_err; echo "rc=$? cwd=$(pwd)")
sleep 0.3
err=$(cat /tmp/ak_err); rm -f /tmp/ak_err
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
check "value hidden"          '[[ "$out" != *abcDEF123456ghiJKL* && "$out" == *"Bearer <concealed by keywarden>"* ]]'
check "base64 form hidden"    '[[ "$out" == *"basic <concealed by keywarden>"* ]]'
check "exit code kept"        '[[ "$out" == *"rc=3"* ]]'
check "cd still applies"      '[[ "$out" == *"cwd=/"* ]]'
check "stderr stays stderr"   '[[ "$err" == *stderr-line* && "$out" != *stderr-line* ]]'
exit $fails
