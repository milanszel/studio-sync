"""Copy a release's installers to the public downloads repo, where the app's
updater finds them.

    python3 packaging/publish_public.py 0.12.0 /path/to/studio-sync-releases

Downloads the installers from the private repo's GitHub release, writes them
to downloads/ in the public repo (older installers are removed), updates
latest.json with sizes and SHA-256 digests, then commits and pushes.
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

CODE_REPO = "milanszel/studio-sync"
PUBLIC_REPO = "milanszel/studio-sync-releases"


def gh(*args, binary=False):
    out = subprocess.run(["gh", *args], check=True, capture_output=True)
    return out.stdout if binary else out.stdout.decode()


def changelog_notes(version):
    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text()
    match = re.search(r"^## %s .*?$(.*?)(?=^## )" % re.escape(version), text, re.S | re.M)
    return match.group(1).strip() if match else ""


def main(version, public):
    public = Path(public)
    release = json.loads(gh("api", "repos/%s/releases/tags/v%s" % (CODE_REPO, version)))
    downloads = public / "downloads"
    downloads.mkdir(exist_ok=True)
    for old in downloads.iterdir():
        old.unlink()
    files = {}
    for asset in release["assets"]:
        name = asset["name"]
        if name.endswith(".exe"):
            key = "windows"
        elif m := re.search(r"-Mac-(arm64|x86_64)\.dmg$", name):
            key = "mac-" + m.group(1)
        else:
            continue
        data = gh(
            "api", "-H", "Accept: application/octet-stream",
            "repos/%s/releases/assets/%d" % (CODE_REPO, asset["id"]), binary=True,
        )
        if len(data) != asset["size"]:
            raise SystemExit("download of %s is incomplete" % name)
        (downloads / name).write_bytes(data)
        files[key] = dict(
            name=name,
            url="https://github.com/%s/raw/main/downloads/%s" % (PUBLIC_REPO, name),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        print("  %s  %d bytes" % (name, len(data)))
    if "windows" not in files or not any(k.startswith("mac-") for k in files):
        raise SystemExit("the release is missing an installer: %s" % sorted(files))
    latest = dict(version=version, notes=changelog_notes(version), files=files)
    (public / "latest.json").write_text(json.dumps(latest, indent=2) + "\n")
    links = "\n".join(
        "* %s: [%s](%s)" % (label, files[k]["name"], files[k]["url"])
        for k, label in (("mac-arm64", "Mac (Apple silicon)"), ("mac-x86_64", "Mac"), ("windows", "Windows"))
        if k in files
    )
    (public / "README.md").write_text(
        "# Studio Sync %s\n\nStudio Sync updates itself from here. To install it the first time:\n\n%s\n\n"
        "## What's new\n\n%s\n" % (version, links, latest["notes"])
    )
    git = lambda *a: subprocess.run(["git", "-C", str(public), *a], check=True)
    git("add", "-A")
    git(
        "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
        "commit", "-m", "Studio Sync %s\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" % version,
    )
    git("push", "origin", "main")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
