"""Self-update from GitHub. Stdlib only, and deliberately cautious:

- only ever talks to the one fixed repo, over the authenticated GitHub API
  (works for a PRIVATE repo with a read-only token, or a public repo with no
  token at all);
- downloads to a temp dir, byte-compiles the new code BEFORE touching the
  install, and aborts on any syntax error;
- backs up the current code so a bad swap can roll back;
- never touches user data (config*.json, quant.db).

Token (only needed if the repo is private): env QUANT_GH_TOKEN or GITHUB_TOKEN,
else config.advanced.json -> "github_token". A fine-grained PAT scoped to
read-only Contents on this one repo is all it needs; it is never logged or
committed.
"""
import io
import os
import shutil
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import ROOT

REPO = "Julianbjrk/poe2-quant"          # fixed; the updater never points elsewhere
API = "https://api.github.com"
VERSION_FILE = ROOT / "VERSION"
REPLACE = ("quant", "quant.py", "VERSION", "README.md")  # code only — never user data
HEADERS = {"User-Agent": f"QuantUpdater/{__version__}",
           "Accept": "application/vnd.github.raw",         # raw file / tarball bytes
           "X-GitHub-Api-Version": "2022-11-28"}


def token_from(cfg=None):
    return (os.environ.get("QUANT_GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            or ((cfg or {}).get("adv", {}) or {}).get("github_token") or "").strip() or None


def _http(url, token=None, timeout=45):
    req = urllib.request.Request(url, headers=dict(HEADERS))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # follows the tarball 302
        return r.read()


def _ver_tuple(s):
    try:
        return tuple(int(x) for x in str(s).strip().split("."))
    except Exception:
        return (0,)


def local_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return __version__


def remote_version(branch, fetch):
    raw = fetch(f"{API}/repos/{REPO}/contents/VERSION?ref={branch}")
    return raw.decode("utf-8").strip()


def check(branch, fetch=None, token=None):
    """-> {current, latest, available, err?}. Never raises."""
    fetch = fetch or (lambda url: _http(url, token))
    cur = local_version()
    try:
        latest = remote_version(branch, fetch)
    except urllib.error.HTTPError as e:
        hint = (" (repo is private — set a GitHub token, see README → Staying current)"
                if e.code in (401, 404) and not token else "")
        return {"current": cur, "latest": None, "available": False, "err": f"HTTP {e.code}{hint}"}
    except Exception as e:
        return {"current": cur, "latest": None, "available": False, "err": str(e)}
    return {"current": cur, "latest": latest,
            "available": _ver_tuple(latest) > _ver_tuple(cur)}


def _validate(src_root, log):
    """Byte-compile every .py in the candidate before we trust it."""
    import py_compile
    pkg = src_root / "quant"
    if not pkg.is_dir() or not (src_root / "quant.py").exists():
        log("  update: downloaded tree is missing quant/ — aborting")
        return False
    for py in list(pkg.rglob("*.py")) + [src_root / "quant.py"]:
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as e:
            log(f"  update: {py.name} failed to compile — aborting ({e.msg.splitlines()[0]})")
            return False
    return True


def apply(branch, fetch=None, token=None, dest=ROOT, log=print):
    """Download branch tarball, validate, back up, swap in. -> {ok, version?, err?}."""
    fetch = fetch or (lambda url: _http(url, token))
    dest = Path(dest)
    try:
        blob = fetch(f"{API}/repos/{REPO}/tarball/{branch}")
    except Exception as e:
        return {"ok": False, "err": f"download failed: {e}"}
    tmp = dest / ".quant_update_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            tops = {m.name.split("/")[0] for m in tar.getmembers() if "/" in m.name or m.isdir()}
            top = sorted(tops)[0]
            _safe_extract(tar, tmp)
        src_root = tmp / top
        if not _validate(src_root, log):
            return {"ok": False, "err": "validation failed; install untouched"}
        new_version = (src_root / "VERSION").read_text(encoding="utf-8").strip()
        backup = dest / ".quant_backup"
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True, exist_ok=True)
        for name in REPLACE:
            src, cur, bak = src_root / name, dest / name, backup / name
            if not src.exists():
                continue
            if cur.exists():
                if cur.is_dir():
                    shutil.copytree(cur, bak)
                    shutil.rmtree(cur)
                else:
                    shutil.copy2(cur, bak)
                    cur.unlink()
            if src.is_dir():
                shutil.copytree(src, cur)
            else:
                shutil.copy2(src, cur)
        log(f"  update: installed {new_version} (backup in {backup.name}/)")
        return {"ok": True, "version": new_version}
    except Exception as e:
        return {"ok": False, "err": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _safe_extract(tar, path):
    base = Path(path).resolve()
    for m in tar.getmembers():
        target = (base / m.name).resolve()
        if base not in target.parents and target != base:
            raise ValueError(f"unsafe path in archive: {m.name}")
    tar.extractall(path)


def restart(argv=None):
    """Replace this process with a fresh one — picks up the new code."""
    import sys
    args = argv if argv is not None else sys.argv
    os.execv(sys.executable, [sys.executable] + args)
