"""
AWS Lambda wrapper for momentum_bot_ratio.py - SECOND parallel instance.

Identical logic to lambda_function_ratio.py (same bot code, same strategy),
just pointed at its OWN separate GitHub state paths so it can run on its own
schedule alongside the first "momentum-bot-ratio" function without either one
double-processing or racing the other over the same files. Same pattern as
how momentum_bot.py's GitHub Actions and Lambda runners were split apart
earlier (state.json vs state_lambda.json).

Env vars required (set as Lambda function configuration, encrypted at rest):
  ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY
  GITHUB_TOKEN   - a fine-grained PAT scoped to this repo, Contents: Read/write
  GITHUB_REPO    - "marqut94-beep/Options-Bot" (owner/repo, no URL)
  GITHUB_BRANCH  - "main" (optional, defaults to main)
"""

import base64
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BOT_STATE_DIR"] = "/tmp"

import momentum_bot_ratio  # noqa: E402  (import after BOT_STATE_DIR is set)

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "marqut94-beep/Options-Bot")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

READ_ONLY_FILES = ["universe.json"]

# SEPARATE paths from lambda_function_ratio.py's state_ratio.json/signals_ratio.json -
# this is what keeps the two parallel functions from colliding.
READ_WRITE_FILE_MAP = {
    "state.json": "state_ratio_paper.json",
    "signals.json": "signals_ratio_paper.json",
}


def github_get(path):
    r = requests.get(f"{GITHUB_API}/{path}", headers=HEADERS,
                      params={"ref": GITHUB_BRANCH}, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def github_put(path, content_str, sha, message):
    body = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(f"{GITHUB_API}/{path}", headers=HEADERS, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _outside_market_hours():
    et_dt = momentum_bot_ratio.now_et()
    if et_dt.weekday() >= 5:
        return True
    minutes = et_dt.hour * 60 + et_dt.minute
    return not (9 * 60 + 25 <= minutes <= 16 * 60 + 5)


def lambda_handler(event, context):
    if _outside_market_hours() and not (event and event.get("forceRun")):
        return {"statusCode": 200, "body": "outside market hours - no-op"}

    for fname in READ_ONLY_FILES:
        content, _ = github_get(fname)
        if content is not None:
            with open(f"/tmp/{fname}", "w", encoding="utf-8") as f:
                f.write(content)
        elif os.path.exists(f"/tmp/{fname}"):
            os.remove(f"/tmp/{fname}")
    for local_fname, github_path in READ_WRITE_FILE_MAP.items():
        content, _ = github_get(github_path)
        if content is not None:
            with open(f"/tmp/{local_fname}", "w", encoding="utf-8") as f:
                f.write(content)
        elif os.path.exists(f"/tmp/{local_fname}"):
            os.remove(f"/tmp/{local_fname}")

    momentum_bot_ratio.main()

    results = {}
    for local_fname, github_path in READ_WRITE_FILE_MAP.items():
        path = f"/tmp/{local_fname}"
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            new_content = f.read()
        current_content, current_sha = github_get(github_path)
        if current_content == new_content:
            results[github_path] = "unchanged"
            continue
        resp = github_put(github_path, new_content, current_sha,
                           f"momentum_bot_ratio.py (paper instance) Lambda run - update {github_path}")
        results[github_path] = resp.get("commit", {}).get("sha", "committed")

    return {"statusCode": 200, "body": json.dumps(results)}
