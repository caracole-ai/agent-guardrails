#!/bin/bash
# DoD v2 tests: hooks/dod-snapshot.py (UserPromptSubmit) + hooks/dod-check.py (Stop).
# Usage: tests/dod-tests.sh   (DOD_SCRIPTS=<folder> to test another copy of the scripts,
#        the repository's hooks/ folder by default)
# Everything is written to a temporary folder outside any git repository, deleted at the end of the run.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="${DOD_SCRIPTS:-$(cd "$HERE/../hooks" && pwd)}"
python3 - "$HERE" "$SCRIPTS" <<'PY'
import json, os, shutil, subprocess, sys, tempfile

here, scripts = sys.argv[1], sys.argv[2]
work = tempfile.mkdtemp(prefix="dod-tests.")
nogit = os.path.join(work, "nogit")
os.makedirs(nogit)
CHECK = os.path.join(scripts, "dod-check.py")
SNAP = os.path.join(scripts, "dod-snapshot.py")
SNAPDIR = os.path.expanduser("~/.claude/.tmp/dod")
results = []


def record(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + detail))


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool_use(name, inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": inp}]}}


def tool_result():
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}


def text(t):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


DONE = "Done."
DOD = "**Verified**: /x. **Not verified, risks**: nothing."
EDIT = {"file_path": "/x", "old_string": "a", "new_string": "b"}


def turn(tool, inp, final=DONE):
    return [user("do it"), tool_use(tool, inp), tool_result(), text(final)]


def write_transcript(name, entries):
    path = os.path.join(work, name + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


def run(script, payload_bytes):
    r = subprocess.run([script], input=payload_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def check(name, entries, expect, session_id="dodv2-nosnap", cwd=here, stop_hook_active=False):
    path = write_transcript(name, entries)
    code, out, err = run(CHECK, json.dumps({"session_id": session_id, "transcript_path": path,
                                            "cwd": cwd, "stop_hook_active": stop_hook_active}).encode())
    reason = ""
    if code != 0:
        got = "exit%d" % code
    elif not out.strip():
        got = "pass"
    else:
        try:
            parsed = json.loads(out)
            got = parsed.get("decision") or "pass"
            reason = parsed.get("reason", "")
        except ValueError:
            got = "invalid-json"
    record(name + " -> " + expect, got == expect and not err.strip(),
           "got=%s stderr=%s out=%s" % (got, err.strip()[:120], out[:120]))
    return reason


print("=== (a) transcript scan ===")
check("edit_without_dod", turn("Edit", EDIT), "block")
check("edit_with_dod", turn("Edit", EDIT, DOD), "pass")
check("bash_sed_i", turn("Bash", {"command": "sed -i 's/a/b/' /x"}), "block")
check("bash_redirect_tmp", turn("Bash", {"command": "echo x > /tmp/y"}), "pass")
check("bash_read_pipe", turn("Bash", {"command": "cat /etc/hosts | grep a"}), "pass")
check("bash_git_commit", turn("Bash", {"command": "git commit -m x"}), "block")
check("mcp_github_push_files", turn("mcp__plugin_github_github__push_files", {}), "block")
check("mcp_jcodemunch_get_symbol", turn("mcp__jcodemunch__get_symbol", {"repo": "r", "symbol_id": "s"}), "pass")
check("mcp_jcodemunch_index_folder", turn("mcp__jcodemunch__index_folder", {"path": "."}), "pass")
check("artifact_read", turn("Artifact", {"action": "read", "url": "u"}), "pass")
check("artifact_without_action", turn("Artifact", {"file_path": "/x.html"}), "block")
check("agent_explore", turn("Agent", {"subagent_type": "Explore", "prompt": "p", "description": "d"}), "pass")
check("agent_general_purpose", turn("Agent", {"subagent_type": "general-purpose", "prompt": "p", "description": "d"}), "block")
check("turn_without_tool", [user("hi"), text(DONE)], "pass")
check("stop_hook_active", turn("Edit", EDIT), "pass", stop_hook_active=True)
check("bypass_keyword", turn("Edit", EDIT, "Done. __bypass_dod__"), "pass")

print("=== (a bis) Bash / MCP edge cases ===")
check("bash_rtk_proxy_ls", turn("Bash", {"command": "rtk proxy ls -la /Users/someone"}), "pass")
check("bash_cd_and_rm_dist", turn("Bash", {"command": "cd /p && rm -rf ./dist"}), "block")
check("bash_var_scratchpad", turn("Bash", {"command": 'S=/private/tmp/x/scratchpad; mkdir -p "$S/a" && echo 1 > "$S/a/f"'}), "pass")
check("bash_heredoc_python_write", turn("Bash", {"command": "python3 - <<'EOF'\nopen('f','w').write('x')\nEOF"}), "block")
check("bash_python_read_only", turn("Bash", {"command": "python3 -c \"import json;print(json.load(open('/etc/x.json')))\""}), "pass")
check("bash_stderr_dup", turn("Bash", {"command": "ls /nope 2>&1 | head -1"}), "pass")
check("bash_multiline_rm", turn("Bash", {"command": "cd /p\nrm -rf ./build"}), "block")
check("bash_apostrophe_heredoc_sed", turn("Bash", {"command": "cat <<'EOF'\nl'audit\nEOF\nsed -i 's/a/b/' f.txt"}), "block")
check("bash_git_status", turn("Bash", {"command": "git status --short && git log --oneline -3"}), "pass")
check("bash_git_tag_list", turn("Bash", {"command": "git tag -l 'v*'"}), "pass")
check("bash_git_stash_list", turn("Bash", {"command": "git stash list"}), "pass")
check("bash_git_C_push", turn("Bash", {"command": "git -C /p push origin main"}), "block")
check("bash_npm_install", turn("Bash", {"command": "npm install lodash"}), "block")
check("bash_npm_test", turn("Bash", {"command": "npm test"}), "pass")
check("bash_sed_n", turn("Bash", {"command": "sed -n '1,5p' f"}), "pass")
check("bash_chmod_tmp", turn("Bash", {"command": "chmod +x /tmp/x.sh"}), "pass")
check("bash_chmod_project", turn("Bash", {"command": "chmod +x ./s.sh"}), "block")
check("bash_tee_project", turn("Bash", {"command": "echo a | tee out.txt"}), "block")
check("bash_for_do_rm", turn("Bash", {"command": "for f in a b; do rm -rf \"$f\"; done"}), "block")
check("bash_claude_plugin_disable", turn("Bash", {"command": "claude plugin disable foo@bar"}), "block")
check("mcp_hostinger_getAttached", turn("mcp__hostinger__VPS_getAttachedPublicKeysV1", {}), "pass")
check("mcp_figma_set_fills", turn("mcp__figma-console__figma_set_fills", {}), "block")
check("mcp_docs_read", turn("mcp__claude_ai_Claude_Docs__read", {}), "pass")
check("mcp_docs_batch", turn("mcp__claude_ai_Claude_Docs__batch", {}), "block")
check("mcp_chrome_navigate", turn("mcp__claude-in-chrome__navigate", {"url": "u"}), "pass")
check("mcp_context7_resolve", turn("mcp__plugin_context7_context7__resolve-library-id", {}), "pass")
check("agent_without_type", turn("Agent", {"prompt": "p", "description": "d"}), "block")
check("agent_guide", turn("Agent", {"subagent_type": "claude-code-guide", "prompt": "p", "description": "d"}), "pass")

print("=== (b) git fingerprint ===")
repo = os.path.join(work, "repo")
shutil.rmtree(repo, ignore_errors=True)
os.makedirs(repo)
subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
with open(os.path.join(repo, "f.txt"), "w") as f:
    f.write("a\n")
with open(os.path.join(repo, ".gitignore"), "w") as f:
    f.write("build/\n")
subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def snapshot(session_id, cwd):
    code, out, err = run(SNAP, json.dumps({"session_id": session_id, "cwd": cwd, "transcript_path": "", "prompt": "x"}).encode())
    data = None
    try:
        with open(os.path.join(SNAPDIR, session_id + ".json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        pass
    return code, out, err, data


code, out, err, snap = snapshot("dodv2-fp", repo)
record("snapshot_git_fingerprint_non_null",
       code == 0 and out.strip() == "{}" and not err and bool(snap) and bool(snap.get("fingerprint"))
       and snap.get("toplevel") == os.path.realpath(repo),
       "code=%s out=%r err=%r snap=%s" % (code, out, err, snap))
with open(os.path.join(repo, "f.txt"), "a") as f:
    f.write("b\n")
reason = check("fp_tracked_change_without_dod", [user("x"), text(DONE)], "block", session_id="dodv2-fp", cwd=repo)
record("fp_tracked_change_trigger_is_fingerprint", "git fingerprint changed" in reason, reason[:160])
check("fp_tracked_change_with_dod", [user("x"), text(DOD)], "pass", session_id="dodv2-fp", cwd=repo)
snapshot("dodv2-fp", repo)
check("fp_no_change", [user("x"), text(DONE)], "pass", session_id="dodv2-fp", cwd=repo)
with open(os.path.join(repo, "new.txt"), "w") as f:
    f.write("n\n")
check("fp_new_untracked_file", [user("x"), text(DONE)], "block", session_id="dodv2-fp", cwd=repo)
snapshot("dodv2-fp", repo)
os.makedirs(os.path.join(repo, "build"))
with open(os.path.join(repo, "build", "out.js"), "w") as f:
    f.write("//\n")
check("fp_gitignored_file_ignored", [user("x"), text(DONE)], "pass", session_id="dodv2-fp", cwd=repo)
sub = os.path.join(repo, "sub")
os.makedirs(sub)
snapshot("dodv2-fp", sub)
with open(os.path.join(repo, "f.txt"), "a") as f:
    f.write("c\n")
check("fp_cwd_subfolder", [user("x"), text(DONE)], "block", session_id="dodv2-fp", cwd=sub)

code, out, err, snap = snapshot("dodv2-nogit", nogit)
record("snapshot_outside_git_fingerprint_null",
       code == 0 and out.strip() == "{}" and not err and bool(snap) and snap.get("fingerprint") is None,
       "code=%s out=%r err=%r snap=%s" % (code, out, err, snap))
check("nogit_edit_falls_back_on_B", turn("Edit", EDIT), "block", session_id="dodv2-nogit", cwd=nogit)
check("nogit_without_tool", [user("x"), text(DONE)], "pass", session_id="dodv2-nogit", cwd=nogit)
check("snapshot_missing_edit", turn("Edit", EDIT), "block", session_id="dodv2-never-seen", cwd=repo)

print("=== (c) robustness ===")
for label, payload in (("empty_stdin", b""), ("invalid_stdin", b"{not json")):
    code, out, err = run(SNAP, payload)
    record("snapshot_" + label, code == 0 and out.strip() == "{}" and not err, "code=%s out=%r err=%r" % (code, out, err))
    code, out, err = run(CHECK, payload)
    record("check_" + label, code == 0 and not out.strip() and not err, "code=%s out=%r err=%r" % (code, out, err))
for script in (SNAP, CHECK):
    with open(script) as f:
        first = f.readline().rstrip("\n")
    record(os.path.basename(script) + "_executable_shebang",
           os.access(script, os.X_OK) and first == "#!/usr/bin/env python3", "x=%s shebang=%r" % (os.access(script, os.X_OK), first))

# cleanup
shutil.rmtree(work, ignore_errors=True)
for sid in ("dodv2-fp", "dodv2-nogit"):
    try:
        os.unlink(os.path.join(SNAPDIR, sid + ".json"))
    except OSError:
        pass
print("TOTAL %d/%d PASS" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
PY
