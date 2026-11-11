"""Run: python test_backends.py

Checks that 1Password and this computer's key store are interchangeable: save, switch both ways, hand keys to
commands during an approval, and wipe the 1Password copies when the approval ends. It uses a fake `op` and a
separate test namespace, so real keys are never touched.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE_OP = r'''
import json, os, sys
db_path = os.environ["FAKE_OP_DB"]
db = json.load(open(db_path)) if os.path.exists(db_path) else {"vaults": {}}
a = sys.argv[1:]
def arg(flag): return a[a.index(flag) + 1]
def done(out=None):
    json.dump(db, open(db_path, "w"))
    if out is not None: print(json.dumps(out))
    sys.exit(0)
if a[:2] == ["vault", "list"]: done([{"name": v} for v in db["vaults"]])
if a[:2] == ["vault", "create"]: db["vaults"][a[2]] = {}; done()
if a[:2] == ["item", "list"]: done([{"title": t} for t in db["vaults"][arg("--vault")]])
if a[:2] == ["item", "create"]:
    item = json.load(sys.stdin); db["vaults"][arg("--vault")][item["title"]] = item["fields"][0]["value"]; done({})
if a[:2] == ["item", "delete"]: db["vaults"][arg("--vault")].pop(a[2]); done()
if a[:2] == ["item", "edit"]:
    v = db["vaults"][arg("--vault")]; v[arg("--title")] = v.pop(a[2]); done()
if os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
    db["saw_token"] = True
    json.dump(db, open(db_path, "w"))
if a[0] == "read":
    _, vault, name, field = a[1].split("//")[1].split("/") if False else ([""] + a[1][5:].split("/"))
    val = db["vaults"].get(vault, {}).get(name)
    if val is None: sys.stderr.write("not found"); sys.exit(1)
    print(val); sys.exit(0)
sys.stderr.write("fake op: unknown " + " ".join(a)); sys.exit(1)
'''


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agent-keys-test-"))
    (tmp / "fakeop.py").write_text(FAKE_OP)
    env = dict(os.environ, AGENT_KEYS_STATE=str(tmp / "state"), AGENT_KEYS_NS="agent-keys-test",
               AGENT_KEYS_OP=f'"{sys.executable}" "{tmp / "fakeop.py"}"', FAKE_OP_DB=str(tmp / "op.json"))
    env.pop("AGENT_KEYS_BACKEND", None)
    ok, bad = [], []

    def keys(*args, stdin=None):
        r = subprocess.run([sys.executable, str(HERE / "keys.py"), *args], input=stdin, capture_output=True,
                           text=True, env=env, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def expect(name, cond):
        (ok if cond else bad).append(name)

    def approve_now(names=("K_ONE", "K_TWO"), minutes=10):  # stands in for a phone approval of these keys
        (tmp / "state").mkdir(exist_ok=True)
        (tmp / "state" / "grants.json").write_text(json.dumps({n: time.time() + minutes * 60 for n in names}))

    sys.path.insert(0, str(HERE))
    os.environ.update(AGENT_KEYS_NS="agent-keys-test", AGENT_KEYS_STATE=str(tmp / "state"))
    import local_store
    try:
        code, out = keys("setup", "local")
        expect("setup local", code == 0 and "Keys are stored in" in out)
        keys("add", "K_ONE", stdin="value-one-1234567890abc")
        expect("save to the computer's store", "K_ONE" in keys("list")[1])

        keys("approvals", "on")  # every key needs a tap from here on in this test
        code, out = keys("switch", "1password")
        expect("switch needs approval", code != 0 and "not approved" in out)
        approve_now(["K_ONE"])
        code, out = keys("switch", "1password")
        expect("switch to 1Password copies keys", code == 0 and "K_ONE: copied" in out)
        keys("add", "K_TWO", stdin="value-two-abcdefghij12345")
        db = json.loads((tmp / "op.json").read_text())
        expect("new keys go to 1Password", db["vaults"]["Agent Keys"].get("K_TWO") == "value-two-abcdefghij12345")

        approve_now(["K_TWO"])
        code, out = keys("env")
        expect("only the approved key is handed over", "export K_TWO=value-two-abcdefghij12345" in out
               and "K_ONE=" not in out)
        approve_now()
        code, out = keys("env")
        expect("approved commands get 1Password keys", "export K_ONE=value-one-1234567890abc" in out
               and "export K_TWO=value-two-abcdefghij12345" in out)
        expect("copies made during approval", set(local_store.names("agent-keys-test-cache")) == {"K_ONE", "K_TWO"})
        code, out = keys("run", "K_TWO", "--", sys.executable, "-c",
                         "import os; v = os.environ['K_TWO']; print('len', len(v)); print(v)")
        expect("keys run with 1Password", code == 0 and "len 25" in out and "<concealed by keywarden>" in out
               and "value-two" not in out)

        # Strict mode: fresh reads from 1Password, no copies, no saved service token.
        local_store.save("OP_SERVICE_ACCOUNT_TOKEN", "fake-service-token-123456")
        code, out = keys("strict", "on")
        expect("strict mode turns on with 1Password", code == 0 and "Strict mode is on" in out)
        expect("strict mode removes the copies", not local_store.names("agent-keys-test-cache"))
        db = json.loads((tmp / "op.json").read_text()); db.pop("saw_token", None)
        (tmp / "op.json").write_text(json.dumps(db))
        code, out = keys("env")
        expect("strict mode still hands over approved keys", "export K_TWO=value-two-abcdefghij12345" in out)
        expect("strict mode makes no copies", not local_store.names("agent-keys-test-cache"))
        expect("strict mode ignores the saved service token",
               not json.loads((tmp / "op.json").read_text()).get("saw_token"))
        code, out = keys("strict", "off")
        expect("turning strict mode off needs approval", code != 0 and "stays on" in out)
        cfg = json.loads((tmp / "state" / "config.json").read_text()); cfg["strict"] = False
        (tmp / "state" / "config.json").write_text(json.dumps(cfg))
        keys("env")
        keys("lock", "K_ONE")
        expect("locking one key wipes only its copy", set(local_store.names("agent-keys-test-cache")) == {"K_TWO"})
        keys("lock")
        expect("lock wipes the copies", not local_store.names("agent-keys-test-cache"))
        expect("no keys after lock", keys("env")[1].strip() == "")

        approve_now()
        keys("env")
        (tmp / "state" / "grants.json").write_text(json.dumps({"K_ONE": time.time() - 1, "K_TWO": time.time() - 1}))
        keys("env")
        expect("copies wiped when the approval runs out", not local_store.names("agent-keys-test-cache"))

        approve_now(["K_ONE", "K_TWO"])
        code, out = keys("switch", "local")
        expect("switch back to the computer's store", code == 0 and "K_TWO: copied" in out)
        expect("keys readable after switching back", local_store.read("K_TWO") == "value-two-abcdefghij12345")
        code, out = keys("env")
        expect("approved commands get local keys", "export K_TWO=value-two-abcdefghij12345" in out)
    finally:
        for ns in ("agent-keys-test", "agent-keys-test-cache"):
            for n in local_store.names(ns):
                local_store.delete(n, ns)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    for n in bad:
        print("WRONG:", n)
    print(f"{len(ok)}/{len(ok) + len(bad)} store cases right")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
