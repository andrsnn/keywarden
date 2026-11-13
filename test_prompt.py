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
    ("here is my weather api key, save this: " + plain, ["WEATHER_API_KEY"]),
    ("save it: " + plain, ["SAVED_KEY"]),
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
    (f"here's my resend api key {plain}", ["RESEND_API_KEY"]),
    (f"my mapbox token is {plain}", ["MAPBOX_TOKEN"]),
    (f"use this key for the weather api: {plain}", ["WEATHER_API_KEY"]),
    (f"this is the stripe test key {plain}, wire it up", ["STRIPE_TEST_KEY"]),
    (f"here's the password for the db: {plain}", ["DB_PASSWORD"]),
    (f"api key: {plain}", ["API_KEY"]),
    (f"the api token is {plain}", ["API_TOKEN"]),
    (f"my service secret is {plain}", ["SERVICE_SECRET"]),
    (f"here's my service credential {plain}", ["SERVICE_CREDENTIAL"]),
    (f"my service credentials are {plain}", ["SERVICE_CREDENTIALS"]),
]
NOTHING = [
    "key OPENAI_API_KEY [OPENAI_API_KEY]",
    "key DEMO_API_KEY [DEMO_API_KEY]",
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
    "fix the bug in src/components/UserProfileCardContainerView.tsx",
    "commit a1b2c3d4e5f60718293a4b5c6d7e8f9012345678 broke it",
    "the token count is 128000",
    "rotate the api key in the dashboard",
    "the key insight is X",
    "add a primary key to the users table",
    "the database row has id 9a8b7c6d-1e2f-4a5b-8c7d-6e5f4a3b2c1d",
    "the primary key for the row is 9a8b7c6d-1e2f-4a5b-8c7d-6e5f4a3b2c1d",
    "the token check broke in commit a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "the key loader is in src/components/UserProfileCardContainerView2.tsx",
    r"the key loader is in src\components\UserProfileCardContainerView2.tsx",
    f"rotate the api key. The unrelated request id is {plain}",
    f"rotate the api key\nThe unrelated request id is {plain}",
    f"the key settings are at https://example.com/{plain}",
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
# Claude Code logs a blocked message after the hook returns; scrub_in_place blanks the key without moving bytes.
import tempfile
from pathlib import Path
fake = rand(30)
log = Path(tempfile.mkdtemp()) / "session.jsonl"
before = f'{{"a":1}}\n{{"content":"Original prompt: save this as X: {fake}"}}\n'
log.write_text(before)
hits = keys.scrub_in_place(log, [fake.encode()])
after = log.read_text()
if hits != 1 or fake in after or len(after) != len(before) or "X: " + "*" * 30 not in after:
    bad.append("scrub_in_place didn't blank the key in place")
log.unlink()
log.parent.rmdir()
for b in bad:
    print("WRONG:", b)
total = len(FOUND) + len(NOTHING) + 3
print(f"{total - len(bad)}/{total} message cases right")
raise SystemExit(1 if bad else 0)
