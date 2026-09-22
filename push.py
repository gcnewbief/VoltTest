import base64
import os
import subprocess
import sys

GIT = r"C:\Users\GCTesting\Devin working\tools\mingit\cmd\git.exe"
HERE = os.path.dirname(os.path.abspath(__file__))

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

# normalise: accept "user/repo", "user/repo.git", or a full URL
repo = repo.strip().rstrip("/")
if "github.com" in repo:
    repo = repo.split("github.com", 1)[1].lstrip("/:")
if repo.endswith(".git"):
    repo = repo[:-4]

auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()

subprocess.run([GIT, "remote", "remove", "origin"], cwd=HERE, capture_output=True)
subprocess.run([GIT, "remote", "add", "origin",
                f"https://github.com/{repo}.git"], cwd=HERE, check=True)

r = subprocess.run(
    [GIT, "-c", f"http.extraHeader=Authorization: Basic {auth}",
     "push", "-u", "origin", "main", "--tags"],
    cwd=HERE, capture_output=True, text=True)
print(r.stdout)
print(r.stderr.replace(token, "***"))
sys.exit(r.returncode)
