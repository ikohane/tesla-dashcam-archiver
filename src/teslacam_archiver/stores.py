"""Where the library lives.

``LocalStore``  any folder this computer can write: an internal or external
                disk, a mounted SMB/NFS share, or a cloud-synced folder
                (Dropbox, Google Drive, OneDrive, iCloud Drive).
``SshStore``    a NAS or server reached over SSH. Files are listed, moved and
                hashed on the server and copied with rsync, so nothing needs
                to be mounted. The server needs a POSIX shell with GNU find,
                cmp, mv, stat, sha256sum and timeout (Synology DSM, Linux).

Both follow the same rules: a move onto an existing file removes the source
only if ``cmp`` says the two are byte-identical (otherwise it is reported as
a conflict and left alone), folders are removed with ``rmdir`` only, and
uploads never overwrite a file that is already there unless asked to.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple


class StoreError(RuntimeError):
    pass


_MOVE_FN = r'''
m() {
  s="$1"; d="$2"
  [ -e "$s" ] || { echo "GONE $s"; return; }
  mkdir -p "$(dirname "$d")" || { echo "ERR $s"; return; }
  if [ -e "$d" ]; then
    if cmp -s "$s" "$d"; then rm -f "$s" && echo "DUP $s" || echo "ERR $s"
    else echo "CONFLICT $s"; fi
  else
    mv -n "$s" "$d" && [ ! -e "$s" ] && echo "OK $s" || echo "ERR $s"
  fi
}
'''


def parse_move_output(out: str) -> Dict[str, Any]:
    res: Dict[str, Any] = {"moved": 0, "dup_removed": 0, "gone": 0, "conflicts": [], "errors": []}
    for line in out.splitlines():
        tag, _, path = line.partition(" ")
        if tag == "OK":
            res["moved"] += 1
        elif tag == "DUP":
            res["dup_removed"] += 1
        elif tag == "GONE":
            res["gone"] += 1
        elif tag == "CONFLICT":
            res["conflicts"].append(path)
        elif tag == "ERR":
            res["errors"].append(path)
    return res


def sha256_file(path: str, chunk: int = 1 << 20) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


class LocalStore:
    """The library as a folder on this computer."""

    remote = False
    name = "local"

    def __init__(self, base: str):
        self.base = os.path.abspath(os.path.expanduser(base)).rstrip("/")

    def abspath(self, rel: str) -> str:
        return rel if rel.startswith("/") else f"{self.base}/{rel}"

    def describe(self) -> str:
        return self.base

    def list(self, rel: str) -> Tuple[float, List[Tuple[str, int, float]]]:
        """Files under ``rel`` as (relative path, size, max(mtime, ctime)).

        ctime matters: a copier may preserve the car's original mtimes, but
        ctime records when a file actually landed here."""
        root = self.abspath(rel)
        out = []
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d != "@eaDir"]
            for fn in fns:
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((os.path.relpath(p, root), st.st_size, max(st.st_mtime, st.st_ctime)))
        return time.time(), out

    def _sh(self, script: str, timeout: int) -> str:
        r = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            raise StoreError(r.stderr.strip()[-500:])
        return r.stdout

    def move_many(self, pairs: List[Tuple[str, str]], chunk: int = 400) -> Dict[str, Any]:
        total = parse_move_output("")
        for i in range(0, len(pairs), chunk):
            script = _MOVE_FN + "".join(
                f"m {shlex.quote(self.abspath(s))} {shlex.quote(self.abspath(d))}\n"
                for s, d in pairs[i:i + chunk])
            part = parse_move_output(self._sh(script, timeout=900))
            for k in ("moved", "dup_removed", "gone"):
                total[k] += part[k]
            total["conflicts"] += part["conflicts"]
            total["errors"] += part["errors"]
        return total

    def rmdir_empty(self, rels: Iterable[str]) -> None:
        for rel in sorted(set(rels), key=lambda r: -r.count("/")):
            try:
                os.rmdir(self.abspath(rel))
            except OSError:
                pass

    def size(self, rel: str) -> Optional[int]:
        try:
            return os.path.getsize(self.abspath(rel))
        except OSError:
            return None

    def pull(self, rels: List[str], dest: str) -> None:
        for rel in rels:
            shutil.copy2(self.abspath(rel), os.path.join(dest, os.path.basename(rel)))

    def push(self, local: str, rel: str) -> None:
        dst = self.abspath(rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".partial"
        shutil.copy2(local, tmp)
        os.replace(tmp, dst)

    def mkdirs(self, rels: Iterable[str]) -> None:
        for rel in rels:
            os.makedirs(self.abspath(rel), exist_ok=True)

    def write_text(self, rel: str, text: str) -> None:
        dst = self.abspath(rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".partial"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, dst)

    def upload_tree(self, local_root: str, rel: str) -> None:
        """Copy a local tree (symlinks followed) under ``rel``; never overwrites."""
        for dp, _dns, fns in os.walk(local_root, followlinks=True):
            for fn in fns:
                src = os.path.join(dp, fn)
                dst = self.abspath(f"{rel}/{os.path.relpath(src, local_root)}")
                if os.path.exists(dst):
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)

    def sha256_many(self, rels: List[str], sizes: Optional[Dict[str, int]] = None
                    ) -> Dict[str, Optional[str]]:
        return {r: sha256_file(self.abspath(r)) for r in rels}


def pick_rsync() -> str:
    """A modern rsync (>= 3, for --protect-args). macOS ships openrsync at
    /usr/bin/rsync, which lacks it; Homebrew's rsync is preferred."""
    for p in ("/opt/homebrew/bin/rsync", "/usr/local/bin/rsync", shutil.which("rsync") or ""):
        if not p or not os.path.isfile(p):
            continue
        try:
            v = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        first = v.splitlines()[0] if v else ""
        if not first or "openrsync" in v or "version 2." in first:
            continue
        return p
    raise StoreError("rsync 3.x not found (on macOS: brew install rsync)")


class SshStore(LocalStore):
    """The library on a NAS or server, driven over SSH.

    Every remote command runs under ``timeout`` and the ssh client sends
    keep-alives, so a dropped connection never leaves a long-running orphan
    on the server (some NAS sshd builds don't kill children on disconnect)."""

    remote = True
    name = "ssh"

    def __init__(self, host: str, base: str, user: Optional[str] = None,
                 key: Optional[str] = None, port: int = 22, jump: Optional[str] = None,
                 connect_timeout: int = 20):
        self.base = base.rstrip("/")
        self.host, self.user, self.port, self.jump = host, user, int(port), jump
        self.key = os.path.expanduser(key) if key else None
        self.connect_timeout = connect_timeout
        self._rsync_bin: Optional[str] = None

    def describe(self) -> str:
        return f"{self.target()}:{self.base}"

    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def ssh_base(self) -> List[str]:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"ConnectTimeout={self.connect_timeout}",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-p", str(self.port)]
        if self.key:
            args += ["-i", self.key, "-o", "IdentitiesOnly=yes"]
        if self.jump:
            args += ["-o", f"ProxyJump={self.jump}"]
        return args + [self.target()]

    def _ssh(self, remote_cmd: str, *, timeout: int, stdin: Optional[str] = None) -> str:
        argv = self.ssh_base() + [f"timeout {timeout} {remote_cmd}"]
        try:
            r = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                               timeout=timeout + 90)
        except subprocess.TimeoutExpired as e:
            raise StoreError(f"ssh timed out: {remote_cmd[:80]}") from e
        if r.returncode != 0:
            raise StoreError(f"ssh rc={r.returncode}: {r.stderr.strip()[-400:]}")
        return r.stdout

    def _sh(self, script: str, timeout: int) -> str:
        return self._ssh("sh -s", timeout=timeout, stdin=script)

    def ping(self) -> bool:
        try:
            return self._ssh("echo ok", timeout=30).strip() == "ok"
        except StoreError:
            return False

    def list(self, rel: str) -> Tuple[float, List[Tuple[str, int, float]]]:
        root = shlex.quote(self.abspath(rel))
        out = self._ssh("sh -c " + shlex.quote(
            f"date +%s; [ -d {root} ] || exit 0; cd {root} && "
            "find . -name @eaDir -prune -o -type f -printf '%s\\t%T@\\t%C@\\t%P\\n'"),
            timeout=300)
        lines = out.splitlines()
        now = float(lines[0]) if lines else time.time()
        files = []
        for line in lines[1:]:
            try:
                size, mtime, ctime, path = line.split("\t", 3)
                files.append((path, int(size), max(float(mtime), float(ctime))))
            except ValueError:
                continue
        return now, files

    def rmdir_empty(self, rels: Iterable[str]) -> None:
        rels = sorted(set(rels), key=lambda r: -r.count("/"))
        if rels:
            self._sh("".join(f"rmdir {shlex.quote(self.abspath(r))} 2>/dev/null\n" for r in rels)
                     + "true\n", timeout=300)

    def size(self, rel: str) -> Optional[int]:
        out = self._ssh("stat -c %s " + shlex.quote(self.abspath(rel)) + " 2>/dev/null; true",
                        timeout=60).strip()
        return int(out) if out.isdigit() else None

    def rsync_e(self) -> str:
        return " ".join(shlex.quote(a) for a in self.ssh_base()[:-1])

    def rsync_target(self, path: str) -> str:
        return f"{self.target()}:{path}"

    def _rsync(self, args: List[str], timeout: int) -> None:
        if self._rsync_bin is None:
            self._rsync_bin = pick_rsync()
        cmd = [self._rsync_bin, "-a", "--partial", "--protect-args", "--timeout=120",
               "-e", self.rsync_e()] + args
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise StoreError(f"rsync rc={r.returncode}: {r.stderr.strip()[-400:]}")

    def pull(self, rels: List[str], dest: str) -> None:
        lst = os.path.join(dest, ".files-from")
        with open(lst, "w") as f:
            f.write("".join(r + "\n" for r in rels))
        try:
            self._rsync(["--no-relative", f"--files-from={lst}",
                         self.rsync_target(self.base + "/"), dest + "/"], timeout=3600)
        finally:
            os.unlink(lst)

    def push(self, local: str, rel: str) -> None:
        self._ssh("mkdir -p " + shlex.quote(os.path.dirname(self.abspath(rel))), timeout=60)
        self._rsync(["--inplace", local, self.rsync_target(self.abspath(rel))], timeout=3600)

    def mkdirs(self, rels: Iterable[str]) -> None:
        self._ssh("mkdir -p " + " ".join(shlex.quote(self.abspath(r)) for r in rels), timeout=60)

    def write_text(self, rel: str, text: str) -> None:
        p = shlex.quote(self.abspath(rel))
        self._ssh("sh -c " + shlex.quote(f"mkdir -p \"$(dirname {p})\" && cat > {p}.partial "
                                         f"&& mv -f {p}.partial {p}"),
                  timeout=120, stdin=text)

    def upload_tree(self, local_root: str, rel: str) -> None:
        self._ssh("mkdir -p " + shlex.quote(self.abspath(rel)), timeout=60)
        self._rsync(["-L", "--ignore-existing", local_root.rstrip("/") + "/",
                     self.rsync_target(self.abspath(rel) + "/")], timeout=86400)

    def sha256_many(self, rels: List[str], sizes: Optional[Dict[str, int]] = None,
                    batch_bytes: int = 3_000_000_000) -> Dict[str, Optional[str]]:
        """sha256 on the server, in batches of ~3 GB, each under a hard timeout."""
        out: Dict[str, Optional[str]] = {r: None for r in rels}
        by_abs = {self.abspath(r): r for r in rels}
        batch: List[str] = []
        acc = 0

        def flush() -> None:
            if not batch:
                return
            script = "".join(f"sha256sum -- {shlex.quote(self.abspath(r))} 2>/dev/null\n"
                             for r in batch) + "true\n"
            for line in self._sh(script, timeout=1800).splitlines():
                h, _, path = line.partition("  ")
                rel = by_abs.get(path)
                if rel is not None and len(h) == 64:
                    out[rel] = h
            batch.clear()

        for r in rels:
            batch.append(r)
            acc += (sizes or {}).get(r, 50_000_000)
            if acc >= batch_bytes:
                flush()
                acc = 0
        flush()
        return out


def make_store(lib: Dict[str, Any]) -> LocalStore:
    backend = (lib.get("backend") or "local").lower()
    if backend == "local":
        return LocalStore(lib["path"])
    if backend == "ssh":
        missing = [k for k in ("host", "path") if not lib.get(k)]
        if missing:
            raise StoreError(f"[library] backend 'ssh' needs {', '.join(missing)}")
        return SshStore(host=lib["host"], base=lib["path"], user=lib.get("user"),
                        key=lib.get("key"), port=int(lib.get("port", 22)), jump=lib.get("jump"))
    raise StoreError(f"unknown [library] backend {backend!r} (use 'local' or 'ssh')")
