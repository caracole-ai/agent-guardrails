#!/usr/bin/env python3
"""
Definition of Done — hook UserPromptSubmit (v2, 2026-09-18).

Prend une empreinte de l'état git du projet au moment du prompt utilisateur.
dod-check.py (hook Stop) la recompare en fin de tour : une empreinte
différente = mutation, quel que soit le chemin d'édition (Edit/Write, Bash
sed/heredoc, outils MCP, sous-agents).

Empreinte = sha256 de :
  git status --porcelain=v1 --untracked-files=all -z
  + git diff HEAD --no-color            (dépôt sans commit : git diff)
  + pour chaque fichier untracked : chemin \\0 taille \\0 mtime_ns
Hors dépôt git : fingerprint null, dod-check retombe sur le scan du transcript.

Stockage : ~/.claude/.tmp/dod/<session_id>.json ; purge des fichiers > 7 jours.
Ne bloque jamais, n'injecte jamais de contexte : imprime toujours {} et sort 0.
Sous-processus git limités à 4 s ; toute exception = no-op silencieux.

Pour débrancher : retirer les deux entrées de hooks (UserPromptSubmit et Stop)
dans ~/.claude/settings.json puis supprimer dod-snapshot.py et dod-check.py.

Test manuel :
  echo '{"session_id":"t","cwd":"/chemin/depot"}' | ./dod-snapshot.py ; cat ~/.claude/.tmp/dod/t.json
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

GIT_TIMEOUT_S = 4.0
SNAPSHOT_DIR = os.path.join(os.path.expanduser("~"), ".claude", ".tmp", "dod")
PURGE_AGE_S = 7 * 86400


def safe_session_id(value):
    """Nom de fichier sûr dérivé du session_id (None si inutilisable)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())[:128]


def snapshot_path(session_id):
    return os.path.join(SNAPSHOT_DIR, session_id + ".json")


def _git(cwd, args):
    """stdout (bytes) d'une commande git ; None si échec, timeout ou git absent."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def compute_fingerprint(cwd):
    """(toplevel, sha256 hex) ; (None, None) si cwd n'est pas dans un dépôt git."""
    if not cwd or not os.path.isdir(cwd):
        return None, None
    top = _git(cwd, ["rev-parse", "--show-toplevel"])
    if not top:
        return None, None
    toplevel = top.decode("utf-8", "surrogateescape").strip()
    if not toplevel:
        return None, None
    status = _git(toplevel, ["status", "--porcelain=v1", "--untracked-files=all", "-z"])
    if status is None:
        return toplevel, None
    diff = _git(toplevel, ["diff", "HEAD", "--no-color"])
    if diff is None:  # dépôt sans commit : HEAD n'existe pas encore
        diff = _git(toplevel, ["diff", "--no-color"]) or b""
    digest = hashlib.sha256()
    digest.update(status)
    digest.update(b"\0--diff--\0")
    digest.update(diff)
    digest.update(b"\0--untracked--\0")
    for entry in status.split(b"\0"):
        if not entry.startswith(b"?? "):
            continue
        rel = entry[3:]
        path = os.path.join(toplevel, rel.decode("utf-8", "surrogateescape"))
        try:
            st = os.stat(path)
            meta = "%d\0%d" % (st.st_size, st.st_mtime_ns)
        except OSError:
            meta = "missing"
        digest.update(rel + b"\0" + meta.encode("utf-8") + b"\0")
    return toplevel, digest.hexdigest()


def write_snapshot(session_id, cwd, toplevel, fingerprint):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    payload = {
        "session_id": session_id,
        "cwd": cwd,
        "toplevel": toplevel,
        "fingerprint": fingerprint,
        "ts": time.time(),
    }
    tmp = snapshot_path(session_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, snapshot_path(session_id))


def purge_old_snapshots(now=None):
    now = time.time() if now is None else now
    try:
        entries = os.scandir(SNAPSHOT_DIR)
    except OSError:
        return
    with entries:
        for entry in entries:
            try:
                if entry.is_file() and now - entry.stat().st_mtime > PURGE_AGE_S:
                    os.unlink(entry.path)
            except OSError:
                continue


def main():
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        data = {}
    try:
        session_id = safe_session_id(data.get("session_id"))
        cwd = data.get("cwd") or os.getcwd()
        if session_id:
            toplevel, fingerprint = compute_fingerprint(cwd)
            write_snapshot(session_id, cwd, toplevel, fingerprint)
        purge_old_snapshots()
    except Exception:  # ne jamais gêner le prompt
        pass
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
