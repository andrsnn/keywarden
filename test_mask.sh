#!/usr/bin/env bash
# Run: bash test_mask.sh   Checks the output filter the hook adds while keys are granted, using the hook's own
# wrapper. A fake key is set in this script only. Prints PASS or FAIL lines, never the fake value.
HERE="$(cd "$(dirname "$0")" && pwd)"
export FAKE_SECRET="abcDEF123456ghiJKL"
export AGENT_KEYS_NAMES=FAKE_SECRET
wrapped() {  # the hook's wrapper for a command, without the line that loads the real keys
  printf '%s' "$1" | python -c "import sys; sys.path.insert(0, r'$HERE'); import hook; print(hook.wrap('Bash', sys.stdin.read()).split('; ', 1)[1])"
}
CMD='cd /; pwd; printf "Authorization: Bearer %s\n" "$FAKE_SECRET"; printf "basic %s\n" "$(printf %s "$FAKE_SECRET" | base64)"; echo stderr-line >&2; (exit 3)'
W="$(wrapped "$CMD")"
out=$(eval "$W" 2>/tmp/ak_err; echo "rc=$? cwd=$(pwd)")
err=$(cat /tmp/ak_err); rm -f /tmp/ak_err
start=$SECONDS
eval "$(wrapped 'sleep 20 & echo started')" >/tmp/ak_bg 2>/dev/null
took=$((SECONDS - start))
kill %1 2>/dev/null; pkill -f "sleep 20" 2>/dev/null
bg=$(cat /tmp/ak_bg); rm -f /tmp/ak_bg
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
check "value hidden"          '[[ "$out" != *abcDEF123456ghiJKL* && "$out" == *"Bearer <concealed by keywarden>"* ]]'
check "base64 form hidden"    '[[ "$out" == *"basic <concealed by keywarden>"* ]]'
check "exit code kept"        '[[ "$out" == *"rc=3"* ]]'
check "cd still applies"      '[[ "$out" == *"cwd=/"* ]]'
check "stderr stays stderr"   '[[ "$err" == *stderr-line* && "$out" != *stderr-line* ]]'
check "output arrives before the command returns" '[[ "$out" == *"cwd=/"* && "$out" == *Bearer* ]]'
check "a background job doesn't hang it" '[[ "$bg" == *started* && $took -lt 10 ]]'
exit $fails
