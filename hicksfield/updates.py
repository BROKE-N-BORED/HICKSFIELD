"""Update check and verified install (v0.5.0).

On launch the app silently asks GitHub's public releases endpoint
for the latest HicksField release and compares its tag against the
running version. The check is one plain GET to a public endpoint:
it sends no identifying information and needs no token, and it
never downloads anything. When the running version is the newest,
nothing happens at all.

When a newer release is out, the app asks. Only when the user taps
Update now does anything download, and the download is verified
before anything is installed: HicksField releases carry no zip
asset and no checksum file, so the check is against the release
itself. The release names the commit it was built from, and
GitHub's source tarball unpacks into a folder named after that
commit (HICKSFIELD-<commit>). The tarball is only installed when
its top folder carries the release's commit, every file sits under
that folder, and the tree looks like HicksField. A download that
fails the check is refused in plain words and nothing is installed.

The install replaces the app tree in place. The venv is never
touched, and the user's films, models, engines, and voices live
outside the app tree (see paths.py), so an update never wipes
them. After the install the app asks whether to restart now or
wait until next time: the running process keeps the old version
in memory either way, and the next launch picks up the new one.

check_for_update is the one-call API the GUI worker uses: it never
raises for a network or parsing problem. A failed check comes back
as state "unknown" with a plain reason, so the GUI can stay quiet
on a startup check and say so plainly on a manual one. Version tags
that do not look like versions are treated the same way, and a local
version newer than the latest release (a dev tree) is simply
"current"; neither is an error.
"""

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from hicksfield.downloader import download
from hicksfield.errors import DownloadError, HicksFieldError

logger = logging.getLogger("hicksfield.updates")

REPO_OWNER = "BROKE-N-BORED"
REPO_NAME = "HICKSFIELD"
LATEST_RELEASE_URL = ("https://api.github.com/repos/%s/%s/releases/latest"
                      % (REPO_OWNER, REPO_NAME))
RELEASES_PAGE_URL = ("https://github.com/%s/%s/releases"
                     % (REPO_OWNER, REPO_NAME))
CHECK_TIMEOUT_S = 8
USER_AGENT = "HicksField"

# The files that must be executable in the installed tree, kept in
# step with installer.EXECUTABLE_FILES: the menu entry runs run.sh,
# and the installer itself must double-click.
EXECUTABLE_FILES = ("install.sh", "run.sh", "installer.py")

# What must be in the tarball for it to look like HicksField.
REQUIRED_FILES = ("hicksfield/__init__.py", "run.sh")

import re  # noqa: E402 - kept with the version helpers below

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


def api_url():
    """The releases endpoint to ask. HICKSFIELD_UPDATE_API_URL
    redirects it, the way the other HICKSFIELD_* variables redirect
    the app dirs: tests and drives point it at a fake server."""
    return os.environ.get("HICKSFIELD_UPDATE_API_URL") or LATEST_RELEASE_URL


def parse_version(text):
    """A version string as a tuple of ints: "0.5" becomes (0, 5),
    "v0.5" the same, or None when it is not a version at all.
    Anything but an optional "v" followed by numbers and dots is
    refused, so a tag like "v0.5-beta" reads as no usable version
    rather than a wrong one."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest, current):
    """True when the latest version is newer than the running one.
    Either side unparsable means no: an unknown version is never
    reported as an update. Shorter tuples are padded with zeros,
    so 0.5 and 0.5.0 compare equal."""
    latest_parsed = parse_version(latest)
    current_parsed = parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    width = max(len(latest_parsed), len(current_parsed))
    latest_padded = latest_parsed + (0,) * (width - len(latest_parsed))
    current_padded = current_parsed + (0,) * (width - len(current_parsed))
    return latest_padded > current_padded


def fetch_latest_release(urlopen=None, url=None):
    """The latest release from GitHub's public endpoint, as
    {"version", "url", "tag", "sha", "tarball_url", "body"}.
    Raises HicksFieldError with a plain reason when the network
    fails, the answer is not JSON, it carries no tag, or it is a
    draft or a test (pre-) release: only finished releases count.
    urlopen is a test seam: pass a stub and no network is
    touched. url redirects the endpoint, for the fake server."""
    import json
    import urllib.error
    import urllib.request

    opener = urlopen or urllib.request.urlopen
    target = url or api_url()
    request = urllib.request.Request(target, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    try:
        with opener(request, timeout=CHECK_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # An HTTP error means GitHub was reached and answered badly:
        # say so, with the status code, instead of blaming the
        # connection.
        logger.info("update check: GitHub answered HTTP %s", exc.code)
        raise HicksFieldError(
            "GitHub answered with an error (HTTP %s)." % exc.code)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise HicksFieldError("The update check timed out.")
        # The raw reason (an errno, a DNS failure) goes in the log
        # only: the user gets plain words, never a raw error.
        logger.info("update check: could not reach GitHub: %s", reason)
        raise HicksFieldError(
            "Could not reach GitHub.",
            "Check the internet connection and try again.")
    except TimeoutError:
        raise HicksFieldError("The update check timed out.")
    except (ValueError, UnicodeDecodeError):
        raise HicksFieldError(
            "The releases page did not answer in a form HicksField "
            "understands.")
    if not isinstance(payload, dict):
        raise HicksFieldError(
            "The releases page did not answer in a form HicksField "
            "understands.")
    if payload.get("draft") or payload.get("prerelease"):
        raise HicksFieldError(
            "The latest release is not a finished release yet.")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise HicksFieldError(
            "The releases page did not name a version.")
    version = parse_version(tag)
    if version is None:
        raise HicksFieldError(
            "The latest release tag did not look like a version: %s"
            % tag)
    sha = payload.get("target_commitish")
    if not isinstance(sha, str) or not sha:
        raise HicksFieldError(
            "The release did not name the commit it was built from, "
            "so the download could not be checked.")
    tarball_url = payload.get("tarball_url")
    if not isinstance(tarball_url, str) or not tarball_url:
        raise HicksFieldError(
            "The release has no source download to install from.")
    url = payload.get("html_url") or RELEASES_PAGE_URL
    body = payload.get("body")
    return {
        "version": ".".join(str(part) for part in version),
        "url": url,
        "tag": tag,
        "sha": sha,
        "tarball_url": tarball_url,
        "body": body if isinstance(body, str) else "",
    }


def check_for_update(current_version, urlopen=None, url=None):
    """One call, three outcomes, never an exception for a network
    or parsing problem:

    {"state": "newer", "latest": "0.6", "url": ..., "sha": ...,
     "tarball_url": ..., "body": ...}: a newer release is out.
    {"state": "current", "latest": "0.5", ...}: the running
     version is the newest (or newer than the latest release, a
     dev tree).
    {"state": "unknown", "latest": None, "url": None,
     "reason": "<plain words>"}: the check could not run.
    """
    try:
        release = fetch_latest_release(urlopen=urlopen, url=url)
    except HicksFieldError as exc:
        return {"state": "unknown", "latest": None, "url": None,
                "sha": None, "tarball_url": None, "body": "",
                "reason": exc.plain_text()}
    result = {"latest": release["version"], "url": release["url"],
              "sha": release["sha"],
              "tarball_url": release["tarball_url"],
              "body": release["body"]}
    if is_newer(release["version"], current_version):
        result["state"] = "newer"
    else:
        result["state"] = "current"
    return result


def download_release(release, dest, progress_cb=None,
                     cancel_event=None):
    """Download the release's source tarball to `dest` (a Path).
    Resumes a partial download, the way the other downloads do.
    Returns the destination Path. Raises DownloadError, in plain
    words, when the download fails."""
    return download(release["tarball_url"], dest,
                    progress_cb=progress_cb, cancel_event=cancel_event)


def verify_tarball(tarball_path, expected_sha):
    """Check the downloaded tarball against the release, before
    anything is installed. Returns the top folder name.

    The tarball must unpack into exactly one top folder, named
    HICKSFIELD-<the release's commit>; every file must sit under
    it; no member may escape it (no absolute paths, no ".."); and
    the tree must look like HicksField. Anything else raises
    HicksFieldError and nothing is installed."""
    tarball_path = Path(tarball_path)
    try:
        archive = tarfile.open(tarball_path, "r:*")
    except (tarfile.TarError, OSError) as exc:
        raise HicksFieldError(
            "The download did not match the release. Nothing was "
            "installed.",
            "The download is not a file HicksField can open. Try "
            "the update again.") from exc
    with archive:
        try:
            members = archive.getmembers()
        except (tarfile.TarError, OSError) as exc:
            raise HicksFieldError(
                "The download did not match the release. Nothing "
                "was installed.",
                "The download could not be read. Try the update "
                "again.") from exc
        if not members:
            raise HicksFieldError(
                "The download did not match the release. Nothing "
                "was installed.",
                "The download was empty. Try the update again.")
        expected_top = "%s-%s" % (REPO_NAME.lower(),
                                  expected_sha.lower())
        top = None
        names = []
        for member in members:
            name = member.name
            if name.startswith("/") or name.startswith("\\"):
                raise HicksFieldError(
                    "The download did not match the release. "
                    "Nothing was installed.",
                    "A file in the download tried to escape its "
                    "folder. Try the update again.")
            parts = name.split("/")
            if ".." in parts:
                raise HicksFieldError(
                    "The download did not match the release. "
                    "Nothing was installed.",
                    "A file in the download tried to escape its "
                    "folder. Try the update again.")
            if top is None:
                top = parts[0]
            elif parts[0] != top:
                raise HicksFieldError(
                    "The download did not match the release. "
                    "Nothing was installed.",
                    "The download did not unpack into one folder. "
                    "Try the update again.")
            names.append("/".join(parts[1:]))
        if top is None or top.lower() != expected_top:
            raise HicksFieldError(
                "The download did not match the release. Nothing "
                "was installed.",
                "The download was not built from the release's "
                "commit. Try the update again.")
        for required in REQUIRED_FILES:
            if required not in names:
                raise HicksFieldError(
                    "The download did not match the release. "
                    "Nothing was installed.",
                    "The download did not look like HicksField. "
                    "Try the update again.")
        return top


def app_root():
    """The app tree: the folder holding the hicksfield package."""
    return Path(__file__).resolve().parent.parent


def _iter_tree_files(root):
    """Every file under `root`, as (relative path, absolute path),
    skipping the venv, caches, and version control: those are
    never part of an update."""
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if parts[0] in ("venv", ".git"):
            continue
        if "__pycache__" in parts or path.suffix == ".pyc":
            continue
        yield rel, path


def install_update(tarball_path, expected_sha, expected_version,
                   root=None, progress_cb=None):
    """Install a verified tarball over the app tree. Returns the
    new version string.

    The tarball is verified again here (defence in depth: this is
    the function that writes files), unpacked into a scratch
    folder, then synced over the app tree: new files copied in,
    files the new version dropped removed. The venv is never
    touched, and neither is anything outside the app tree: films,
    models, engines, and voices all live elsewhere. The install is
    refused, in plain words, when this copy was not installed with
    the installer (no venv) or the app folder cannot be written.
    After the copy, the new version is started once to confirm it
    reports the expected version; a failed confirmation is an
    error, not a silent half-install."""
    from hicksfield import paths as paths_mod

    root = Path(root) if root is not None else app_root()
    venv_python = root / "venv" / "bin" / "python"
    if not venv_python.exists():
        raise HicksFieldError(
            "This copy of HicksField was not installed with the "
            "installer, so it cannot update itself.",
            "Download the new release from the releases page and "
            "run the installer again.")
    if not os.access(root, os.W_OK):
        raise HicksFieldError(
            "HicksField cannot write to its own folder, so the "
            "update cannot be installed.",
            "Check the folder's permissions and try again.")
    top = verify_tarball(tarball_path, expected_sha)
    if progress_cb is not None:
        progress_cb("Checking the download.", 1.0, "")
    scratch_parent = paths_mod.data_dir() / "updates"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="hicksfield-update-",
                                    dir=str(scratch_parent)))
    try:
        with tarfile.open(tarball_path, "r:*") as archive:
            archive.extractall(path=scratch, filter="data")
        new_tree = scratch / top
        if not new_tree.is_dir():
            raise HicksFieldError(
                "The download did not match the release. Nothing "
                "was installed.",
                "The download did not unpack the way it should. "
                "Try the update again.")
        new_files = dict(_iter_tree_files(new_tree))
        if progress_cb is not None:
            progress_cb("Installing the update.", 0.2,
                        "Copying the new files.")
        # Files the new version dropped go first, so a file that
        # became a folder (or the other way round) never collides.
        for rel, path in _iter_tree_files(root):
            if rel not in new_files:
                path.unlink()
        for rel, src in new_files.items():
            target = root / rel
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            if target.parent.is_file():
                target.parent.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(os.readlink(src))
            else:
                shutil.copy2(src, target)
        # Prune folders left empty by dropped files, bottom up.
        for folder in sorted(
                (p for p in root.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts), reverse=True):
            if folder == root or folder.parts[0:1] == ("venv",):
                continue
            if folder.relative_to(root).parts[0] == "venv":
                continue
            try:
                folder.rmdir()
            except OSError:
                pass
        for name in EXECUTABLE_FILES:
            script = root / name
            if script.exists():
                script.chmod(script.stat().st_mode | 0o111)
        if progress_cb is not None:
            progress_cb("Installing the update.", 0.9,
                        "Checking the new version starts.")
        try:
            proc = subprocess.run(
                [str(venv_python), "-m", "hicksfield", "--version"],
                cwd=str(root), capture_output=True, text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HicksFieldError(
                "The update is installed, but HicksField could not "
                "start to confirm the new version.",
                "Run the installer again from the release page.") \
                from exc
        if proc.returncode != 0 or (
                "HicksField %s" % expected_version) not in proc.stdout:
            raise HicksFieldError(
                "The update is installed, but HicksField could not "
                "confirm the new version.",
                "Run the installer again from the release page.")
        return expected_version
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def restart_app():
    """Restart the app in place: replace this process with a fresh
    HicksField, so the new version runs. Never returns on success.
    The caller closes the windows and gives up the single-instance
    lock first (run_app does that after the event loop ends)."""
    sys.stdout.flush()
    sys.stderr.flush()
    argv = [sys.executable, "-m", "hicksfield"] + sys.argv[1:]
    os.execv(sys.executable, argv)
