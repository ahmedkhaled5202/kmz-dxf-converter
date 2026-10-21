# -*- coding: utf-8 -*-
"""
Automation script: upload project to GitHub, trigger build, download the EXE.
Prerequisites:
  1. gh CLI installed and authenticated (run: gh auth login)
  2. git installed
  3. Both kmz_dxf_converter_v2.py and kmz_dxf_gui.py in the same folder as this script

Usage:
  python build_to_github.py <repo-name>
  e.g.  python build_to_github.py kmz-dxf-converter
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CORE_FILE = "kmz_dxf_converter_v2.py"
GUI_FILE = "kmz_dxf_gui.py"
REQUIREMENTS = "requirements.txt"
WORKFLOW = ".github/workflows/build.yml"

REQUIREMENTS_CONTENT = "ezdxf>=1.1\npyproj\n"

WORKFLOW_CONTENT = """name: Build EXE
on: [push, workflow_dispatch]
jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
          cache: 'pip'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          pip install pyinstaller

      - name: Build EXE
        run: pyinstaller --onefile --windowed --name kmz_dxf_gui --hidden-import kmz_dxf_converter_v2 kmz_dxf_gui.py

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: kmz_dxf_exe
          path: dist/kmz_dxf_gui.exe
          if-no-files-found: error
"""


def run(cmd, check=True, cwd=None, capture=False):
    """Run a shell command. If capture=True, returns stdout."""
    print(f"  $ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(
        cmd, shell=isinstance(cmd, str), cwd=cwd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True
    )
    if check and result.returncode != 0:
        err = result.stderr if capture else ""
        print(f"\n❌ Command failed (exit {result.returncode})")
        if err:
            print(err)
        sys.exit(1)
    return result.stdout.strip() if capture else None


def check_prereqs():
    print("🔍 Checking prerequisites...")
    for tool in ("gh", "git"):
        result = subprocess.run(["which", tool], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"❌ Missing tool: {tool}. Install it first.")
            sys.exit(1)
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if result.returncode != 0:
        print("❌ Not logged in to GitHub. Run: gh auth login")
        sys.exit(1)
    print("  ✅ gh and git found, authenticated.")


def check_files():
    print("\n🔍 Checking required files...")
    here = Path(__file__).parent
    missing = []
    for f in (CORE_FILE, GUI_FILE):
        if not (here / f).exists():
            missing.append(f)
    if missing:
        print(f"❌ Missing files: {', '.join(missing)}")
        print(f"  Put them in the same folder as this script: {here}")
        sys.exit(1)
    print(f"  ✅ Found {CORE_FILE} and {GUI_FILE}.")


def write_helper_files():
    print("\n📝 Writing helper files...")
    here = Path(__file__).parent
    req = here / REQUIREMENTS
    if not req.exists() or req.read_text().strip() == "":
        req.write_text(REQUIREMENTS_CONTENT)
        print(f"  ✅ Created {REQUIREMENTS}")
    else:
        print(f"  ✓ {REQUIREMENTS} exists, leaving as-is.")

    wf = here / WORKFLOW
    wf.parent.mkdir(parents=True, exist_ok=True)
    if not wf.exists() or wf.read_text().strip() == "":
        wf.write_text(WORKFLOW_CONTENT)
        print(f"  ✅ Created {WORKFLOW}")
    else:
        print(f"  ✓ {WORKFLOW} exists, leaving as-is.")


def get_github_user():
    out = run(["gh", "api", "user", "--jq", ".login"], capture=True)
    return out


def create_repo(repo_name):
    print(f"\n🌐 Creating GitHub repository: {repo_name}")
    user = get_github_user()
    full = f"{user}/{repo_name}"
    # Check if exists
    result = subprocess.run(["gh", "repo", "view", full], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  ℹ️  Repository {full} already exists, using it.")
    else:
        run(["gh", "repo", "create", full, "--public", "--description", "KMZ DXF Converter", "--confirm"])
        print(f"  ✅ Created {full}")
    return user, full


def init_git(repo_full):
    print("\n📦 Setting up git...")
    here = Path(__file__).parent
    if not (here / ".git").exists():
        run(["git", "init"])
        print("  ✅ git init done")
    run(["git", "config", "user.name", get_github_user()])
    run(["git", "config", "user.email", f"{get_github_user()}@users.noreply.github.com"])
    run(["git", "add", "-A"])
    # Commit only if changes
    result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
    if result.returncode != 0:
        run(["git", "commit", "-m", "Build: KMZ DXF Converter with GUI"])
        print("  ✅ Committed.")
    else:
        print("  ℹ️  Nothing to commit.")
    run(["git", "branch", "-M", "main"])
    # Setup remote
    result = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True)
    if result.returncode == 0:
        run(["git", "remote", "set-url", "origin", f"https://github.com/{repo_full}.git"])
    else:
        run(["git", "remote", "add", "origin", f"https://github.com/{repo_full}.git"])
    print("  ✅ Remote configured.")


def push():
    print("\n🚀 Pushing to GitHub...")
    run(["gh", "auth", "setup-git"])
    run(["git", "push", "-u", "origin", "main", "--force"])
    print("  ✅ Pushed.")


def trigger_build():
    print("\n⚙️  Triggering build...")
    run(["gh", "workflow", "run", "build.yml"])
    time.sleep(5)
    run_id = run([
        "gh", "run", "list", "--limit", "1",
        "--json", "databaseId", "--jq", ".[0].databaseId"
    ], capture=True)
    print(f"  ✅ Run started: {run_id}")
    return run_id


def watch_build(run_id):
    print(f"\n👀 Watching build {run_id} (this may take 3-8 minutes)...")
    run(["gh", "run", "watch", run_id])


def download_exe(run_id):
    print(f"\n⬇️  Downloading artifact...")
    here = Path(__file__).parent
    # Clean old
    for old in (here / "kmz_dxf_exe").glob("*"):
        old.unlink()
    run(["gh", "run", "download", run_id, "-n", "kmz_dxf_exe"], cwd=here)
    # Find the exe
    exe = None
    for candidate in (here / "kmz_dxf_exe").rglob("*.exe"):
        exe = candidate
        break
    if not exe:
        print("❌ No EXE found in the downloaded artifact.")
        sys.exit(1)
    final = here / "kmz_dxf_gui.exe"
    if final.exists():
        final.unlink()
    exe.rename(final)
    print(f"  ✅ Saved as: {final}")
    print(f"  📦 Size: {final.stat().st_size / 1024 / 1024:.1f} MB")


def main():
    if len(sys.argv) < 2:
        print("Usage: python build_to_github.py <repo-name>")
        print("Example: python build_to_github.py kmz-dxf-converter")
        sys.exit(1)
    repo_name = sys.argv[1]

    print("=" * 60)
    print(f" Build KMZ ↔ DXF Converter → EXE via GitHub Actions")
    print(f" Repo name: {repo_name}")
    print("=" * 60)

    check_prereqs()
    check_files()
    write_helper_files()
    user, full = create_repo(repo_name)
    init_git(full)
    push()
    run_id = trigger_build()
    watch_build(run_id)
    download_exe(run_id)

    print("\n" + "=" * 60)
    print("✅ DONE! Your EXE is ready in the current folder.")
    print("=" * 60)


if __name__ == "__main__":
    main()