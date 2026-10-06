"""Create a GitHub release for the current APP_VERSION and upload
dist\\VoltCheck.exe. Uses GITHUB_TOKEN / GITHUB_REPO from .env (same as
push.py). Usage:  py release.py [tag]   (default tag: v<APP_VERSION>)
"""
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "VoltCheck.exe")

env = {}
for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

repo = env.get("GITHUB_REPO", "")
token = env.get("GITHUB_TOKEN", "")
if not repo or not token:
    sys.exit("GITHUB_REPO or GITHUB_TOKEN missing from .env")
repo = repo.strip().rstrip("/")
if "github.com" in repo:
    repo = repo.split("github.com", 1)[1].lstrip("/:")
if repo.endswith(".git"):
    repo = repo[:-4]

tag = sys.argv[1] if len(sys.argv) > 1 else ""
if not tag:
    src = open(os.path.join(HERE, "battery_monitor.py"), encoding="utf-8").read()
    tag = "v" + re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src).group(1)

if not os.path.exists(EXE):
    sys.exit(f"{EXE} not found - run build.bat first")


def api(url, method="GET", data=None, headers=None, raw=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        if raw is not None:
            resp = urllib.request.urlopen(req, raw)
        else:
            body = json.dumps(data).encode() if data is not None else None
            req.add_header("Content-Type", "application/json")
            resp = urllib.request.urlopen(req, body)
        return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        print(e.read().decode()[:500])
        raise


base = f"https://api.github.com/repos/{repo}"

# reuse an existing release for this tag, else create one
try:
    rel = api(f"{base}/releases/tags/{tag}")
    print(f"release {tag} already exists - uploading asset to it")
except urllib.error.HTTPError:
    rel = api(f"{base}/releases", "POST", {
        "tag_name": tag,
        "name": f"VoltCheck {tag}",
        "body": ("Battery drain-test & health monitor for Windows.\n\n"
                 "- No dead-cell faults below 10% (normal cell behaviour)\n"
                 "- Auto-saves the report when the battery crosses 10%\n"
                 "- OPEN LOG: reload a saved drain CSV to review or continue\n\n"
                 "Portable exe - no Python required. `VoltCheck.exe --demo` "
                 "runs a simulated drain."),
        "draft": False,
        "prerelease": False,
    })
    print(f"created release {rel['html_url']}")

upload_url = rel["upload_url"].split("{")[0]
with open(EXE, "rb") as f:
    asset = api(f"{upload_url}?name=VoltCheck.exe", "POST",
                headers={"Content-Type": "application/octet-stream"},
                raw=f.read())
print(f"uploaded: {asset['browser_download_url']}")
