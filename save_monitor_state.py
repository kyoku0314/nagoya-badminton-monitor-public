#!/usr/bin/env python3
"""Persist monitor state without overwriting concurrent repository changes."""
import json
import subprocess
import time
from pathlib import Path

FILES = ("seen.json", "failed_dates.json")

def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, text=True, capture_output=True)

def read_remote(name):
    result = git("show", f"origin/main:{name}", check=False)
    if result.returncode:
        return {} if name == "seen.json" else []
    return json.loads(result.stdout)

def main():
    local_seen = json.loads(Path("seen.json").read_text(encoding="utf-8"))
    local_failed = json.loads(Path("failed_dates.json").read_text(encoding="utf-8"))
    checked_path = Path("checked_dates_run.json")
    local_checked = set(json.loads(checked_path.read_text(encoding="utf-8"))) if checked_path.exists() else set()
    if not isinstance(local_seen, dict) or not isinstance(local_failed, list):
        raise ValueError("Unexpected monitor state format")
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    for attempt in range(1, 4):
        git("fetch", "origin", "main")
        remote_seen = read_remote("seen.json")
        remote_failed = read_remote("failed_dates.json")
        if not isinstance(remote_seen, dict) or not isinstance(remote_failed, list):
            raise ValueError("Unexpected remote monitor state format")
        # Retain notifications from both runs; never discard existing records.
        merged_seen = {**remote_seen, **local_seen}
        # Keep failures from concurrent runs, but clear dates verified by this run.
        # An unverified date is never removed from the retry queue.
        merged_failed = sorted((set(remote_failed) | set(local_failed)) - local_checked)
        # Rebase only the two generated state files onto the newest remote commit.
        git("reset", "--hard", "origin/main")
        Path("seen.json").write_text(json.dumps(merged_seen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        Path("failed_dates.json").write_text(json.dumps(merged_failed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git("add", *FILES)
        if not git("diff", "--cached", "--quiet", check=False).returncode:
            committed = git("commit", "-m", "Update monitor state [skip ci]", check=False)
            if committed.returncode:
                # A concurrent index update or an empty commit must not be
                # mistaken for a monitoring failure. Keep genuine errors visible.
                pending = git("diff", "--cached", "--quiet", check=False)
                if pending.returncode == 0:
                    print("No state changes remain to commit")
                    return
                print(f"Commit attempt {attempt} failed (exit {committed.returncode})")
                print("stdout:", committed.stdout.strip())
                print("stderr:", committed.stderr.strip())
                if attempt == 3:
                    raise RuntimeError("Unable to commit monitor state")
                time.sleep(attempt * 2)
                continue
            pushed = git("push", "origin", "HEAD:main", check=False)
            if pushed.returncode == 0:
                print("Monitor state saved successfully")
                return
            print(f"Push attempt {attempt} failed: {pushed.stderr.strip()}")
        else:
            print("State already up to date")
            return
        time.sleep(attempt * 2)
    raise RuntimeError("Unable to save monitor state after 3 attempts")

if __name__ == "__main__":
    main()
