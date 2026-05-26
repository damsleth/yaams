"""Stand-alone proof-of-concept for the Teams chatsvc ingest path.

Why this exists: tenants like SoftwareOne gate Microsoft Graph's
`/me/chats` behind a Conditional Access policy that requires
Intune-managed devices, which makes the Graph-based Teams adapter in
yaams unusable for that profile. Teams web itself bypasses this by
talking to `teams.microsoft.com/api/chatsvc/<region>/v1/...` with an
access token whose audience is `ic3.teams.office.com` - that path has a
softer CA policy on most tenants. owa-piggy can mint such a token using
its existing FOCI refresh token without re-auth.

This script demonstrates the end-to-end flow: acquire token, list
conversations, sample messages, print counts. No content is dumped, so
it's safe to commit example output. Run:

    python3 scripts/poc_chatsvc.py <profile> [--region emea] [--max-convos 20]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse
import urllib.request

CHATSVC_HOST = "https://teams.microsoft.com"
DEFAULT_VIEW = "msnp24Equivalent|supportsMessageProperties"


def get_token(profile: str) -> str:
    result = subprocess.run(
        ["owa-piggy", "--profile", profile, "--audience", "ic3"],
        capture_output=True, text=True, check=True,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(f"owa-piggy returned empty token for profile {profile}")
    return token


def chatsvc_get(token: str, url: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def iter_conversations(token: str, region: str, page_size: int = 50):
    """Yield conversations, following backwardLink until exhausted.

    chatsvc paginates with `_metadata.backwardLink` (older items) and
    `_metadata.forwardLink` (newer items). For an initial bulk pull we
    walk backwardLink; for incremental sync we'd persist `syncState`
    and pass it on next run.
    """
    url = (
        f"{CHATSVC_HOST}/api/chatsvc/{region}/v1/users/ME/conversations"
        f"?pageSize={page_size}&view={urllib.parse.quote(DEFAULT_VIEW)}"
    )
    while url:
        data = chatsvc_get(token, url)
        for c in data.get("conversations", []):
            yield c
        url = (data.get("_metadata") or {}).get("backwardLink") or ""


def iter_messages(token: str, region: str, chat_id: str, page_size: int = 50):
    cid = urllib.parse.quote(chat_id, safe="")
    url = (
        f"{CHATSVC_HOST}/api/chatsvc/{region}/v1/users/ME/conversations/{cid}/messages"
        f"?pageSize={page_size}&view={urllib.parse.quote(DEFAULT_VIEW)}"
    )
    while url:
        data = chatsvc_get(token, url)
        for m in data.get("messages", []):
            yield m
        url = (data.get("_metadata") or {}).get("backwardLink") or ""


def summarize(profile: str, region: str, max_convos: int):
    token = get_token(profile)
    convos_seen = 0
    text_msgs = 0
    system_msgs = 0
    topics_sample = []
    for c in iter_conversations(token, region):
        if convos_seen >= max_convos:
            break
        convos_seen += 1
        topic = (c.get("threadProperties") or {}).get("topic", "") or "(no topic)"
        thread_type = (c.get("threadProperties") or {}).get("threadType", "")
        if len(topics_sample) < 5 and topic != "(no topic)":
            topics_sample.append(f"{thread_type}: {topic[:60]}")
        # Sample 20 messages from each convo (enough to see types/counts
        # without committing to a full backfill).
        seen = 0
        for m in iter_messages(token, region, c["id"], page_size=20):
            seen += 1
            mtype = m.get("messagetype", "")
            if mtype == "Text" or mtype == "RichText/Html":
                text_msgs += 1
            else:
                system_msgs += 1
            if seen >= 20:
                break
    print(f"profile={profile} region={region}")
    print(f"  conversations sampled: {convos_seen}")
    print(f"  text messages: {text_msgs}")
    print(f"  system/activity messages: {system_msgs}")
    print(f"  topic samples (first {len(topics_sample)}):")
    for t in topics_sample:
        print(f"    - {t}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--region", default="emea")
    ap.add_argument("--max-convos", type=int, default=20)
    args = ap.parse_args()
    try:
        summarize(args.profile, args.region, args.max_convos)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        print(f"HTTP {e.code} from chatsvc: {body}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
