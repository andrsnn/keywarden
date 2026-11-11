"""Run: python test_prompt.py   Checks which keys get found in a message, and their names. Fake keys are made at run time."""
import random
import string

import keys

rnd = random.Random(7)


def rand(n, chars=string.ascii_letters + string.digits):
    return "".join(rnd.choice(chars) for _ in range(n))


hexs = "0123456789abcdef"
openai = "sk-proj-" + rand(48)
gh = "ghp_" + rand(36)
cf = "cfat_" + rand(48)
r2_id, r2_secret = rand(32, hexs), rand(64, hexs)
plain = rand(40)
pem = "-----BEGIN RSA PRIVATE KEY-----\n" + rand(64) + "\n" + rand(64) + "\n-----END RSA PRIVATE KEY-----"

FOUND = [  # (message, the names it saves)
    ("key CLOUDFRONT_KEY_PAIR_ID K2JCJMDEHXQW5F", ["CLOUDFRONT_KEY_PAIR_ID"]),
    ("key cloudfront_key " + plain, ["CLOUDFRONT_KEY"]),
    ("  key R2_ACCESS_KEY_ID " + r2_id + "\nkey R2_SECRET " + r2_secret, ["R2_ACCESS_KEY_ID", "R2_SECRET"]),
    ("key PIN hunter2", ["PIN"]),
    ("key CLOUDFRONT_PRIVATE_KEY\n" + pem, ["CLOUDFRONT_PRIVATE_KEY"]),
    # "save this"
    ("save this cloudfront key K2JCJMDEHXQW5F", ["CLOUDFRONT_KEY"]),
    ("save this K2JCJMDEHXQW5F as CLOUDFRONT_KEY_PAIR_ID", ["CLOUDFRONT_KEY_PAIR_ID"]),
    ("K2JCJMDEHXQW5F save this", ["SAVED_KEY"]),
    ("save my cloud front key pair id K2JCJMDEHXQW5F", ["CLOUD_FRONT_KEY_PAIR_ID"]),
    ("can you store this R2_SECRET_ACCESS_KEY=" + r2_secret, ["R2_SECRET_ACCESS_KEY"]),
    ("save this " + openai, ["OPENAI_API_KEY"]),
    ("save this as CLOUDFRONT_PRIVATE_KEY\n" + pem, ["CLOUDFRONT_PRIVATE_KEY"]),
    # found with no "save"
    (f"here's my openai key {openai}", ["OPENAI_API_KEY"]),
    (f"use this {gh} for the repo", ["GITHUB_TOKEN"]),
    (pem, ["PRIVATE_KEY"]),
    ("OPENAI_API_KEY=" + plain + "\nR2_BUCKET=games", ["OPENAI_API_KEY"]),
]
NOTHING = [
    f"the api token is {plain}",
    'HTTP 429 rate limit, request id: req_011CVaB3dE5fG7hJ9kL1mN3pQ5r. which key is it using?',
    "use this key for cloud front K2JCJMDEHXQW5F",
    "key point: the loader is slow",
    "the key point is the loader at 2026-11-11T08:29:08.952Z",
    "fix the bug in src/world/gearfit.ts line 40 and push",
    "commit 3f9c2b8e7d6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e looks wrong",
    "set MAX_TOKENS=4096 in the config",
    "save the file and push to main",
    "save this as a draft and open https://example.com/page?id=12345678abc",
    "store R2_BUCKET in the config",
    "save this as draft please",
]

bad = []
for text, want in FOUND:
    names = [n for n, _, _ in keys.find_keys(text)]
    if names != want:
        bad.append(f"wanted {want}, got {names}: {text[:50]!r}")
for text in NOTHING:
    got = keys.find_keys(text)
    if got:
        bad.append(f"found {[n for n, _, _ in got]} in: {text[:50]!r}")
if keys.key_in(f'client = OpenAI(api_key="{openai}")') != "OPENAI_API_KEY":
    bad.append("key_in missed a key written into code")
if keys.key_in('client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])'):
    bad.append("key_in flagged code that reads an environment variable")
for b in bad:
    print("WRONG:", b)
total = len(FOUND) + len(NOTHING) + 2
print(f"{total - len(bad)}/{total} message cases right")
raise SystemExit(1 if bad else 0)
