"""
setup_wizard.py — one-time guided setup for the transcript analyzer.

Walks through dependency checks, folder configuration, and writes .env.
Run directly or invoked automatically by run.py on first use.

Usage:
    python setup_wizard.py
    python setup_wizard.py --reset    # re-run even if setup_complete.json exists
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Rich is not yet guaranteed to be installed when this runs, so import it
# lazily after the dependency install step. Plain print() is used before that.

_HERE = Path(__file__).parent
SETUP_MARKER = _HERE / "setup_complete.json"
ENV_PATH = _HERE / ".env"
ENV_EXAMPLE = _HERE / ".env.example"
CONTEXT_DIR = _HERE / "client_context"
REQUIREMENTS = _HERE / "requirements.txt"


# ---------------------------------------------------------------------------
# Pre-rich helpers (used before we know rich is installed)
# ---------------------------------------------------------------------------

def _plain_header(text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def _plain_step(n: int, total: int, text: str) -> None:
    print(f"\n[{n}/{total}] {text}")


def _plain_ok(text: str) -> None:
    print(f"  OK  {text}")


def _plain_warn(text: str) -> None:
    print(f"  !!  {text}")


def _plain_fail(text: str) -> None:
    print(f"  XX  {text}")


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

TOTAL_STEPS = 9


def step_python_version(console=None) -> bool:
    ok = sys.version_info >= (3, 9)
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if console:
        style = "green" if ok else "red"
        console.print(f"  Python {ver}", style=style)
        if not ok:
            console.print("  Python 3.9 or higher is required.", style="red")
    else:
        if ok:
            _plain_ok(f"Python {ver}")
        else:
            _plain_fail(f"Python {ver} — 3.9+ required")
    return ok


def step_install_deps(console=None) -> bool:
    msg = "Installing Python dependencies from requirements.txt ..."
    if console:
        console.print(f"  {msg}")
    else:
        print(f"  {msg}")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        capture_output=False,
        text=True,
    )
    ok = result.returncode == 0
    if console:
        if ok:
            console.print("  Dependencies installed.", style="green")
        else:
            console.print("  pip install failed — check output above.", style="red")
    else:
        if ok:
            _plain_ok("Dependencies installed.")
        else:
            _plain_fail("pip install failed.")
    return ok


def step_ffmpeg(console=None) -> bool:
    while True:
        found = shutil.which("ffmpeg") is not None
        if found:
            if console:
                console.print("  ffmpeg found in PATH.", style="green")
            else:
                _plain_ok("ffmpeg found in PATH.")
            return True

        msg = (
            "ffmpeg not found. It is required for MP4 transcription.\n"
            "  Install it with:  winget install Gyan.FFmpeg\n"
            "  Then close and reopen this terminal window."
        )
        if console:
            console.print(f"  {msg}", style="yellow")
        else:
            _plain_warn(msg)

        answer = input("\n  Press Enter after installing ffmpeg, or type 'skip' to continue without it: ").strip().lower()
        if answer == "skip":
            if console:
                console.print("  Skipping ffmpeg — MP4 transcription will not work.", style="yellow")
            else:
                _plain_warn("Skipping ffmpeg — MP4 transcription will not work.")
            return False


def _copilot_executable() -> str:
    """Mirror of analyze_copilot.py's _copilot_executable() — kept local to avoid import."""
    candidates: list[str] = []
    if os.name == "nt":
        app_data = os.environ.get("APPDATA", "")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        user_home = str(Path.home())
        candidates.extend([
            os.path.join(local_app_data, "Microsoft", "WinGet", "Packages",
                         "GitHub.Copilot_Microsoft.Winget.Source_8wekyb3d8bbwe", "copilot.exe"),
            os.path.join(user_home, "AppData", "Local", "Microsoft", "WinGet", "Links", "copilot.exe"),
            os.path.join(app_data, "Code", "User", "globalStorage",
                         "github.copilot-chat", "copilotCli", "copilot"),
            os.path.join(app_data, "Code", "User", "globalStorage",
                         "github.copilot-chat", "copilotCli", "copilot.bat"),
        ])
    for c in candidates:
        if c and os.path.exists(c):
            return c
    cp = shutil.which("copilot")
    if cp:
        return cp
    return "copilot"


def step_copilot_cli(console=None) -> bool:
    exe = _copilot_executable()
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=15,
        )
        ok = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        ok = False

    if ok:
        if console:
            console.print("  GitHub Copilot CLI found.", style="green")
        else:
            _plain_ok("GitHub Copilot CLI found.")
    else:
        msg = (
            "GitHub Copilot CLI not found.\n"
            "  Install it:  winget install GitHub.Copilot\n"
            "  Then restart this terminal and re-run setup."
        )
        if console:
            console.print(f"  {msg}", style="red")
        else:
            _plain_fail(msg)
    return ok


def step_copilot_auth(console=None) -> bool:
    exe = _copilot_executable()
    msg = "  Checking Copilot authentication (sending a test prompt) ..."
    if console:
        console.print(msg)
    else:
        print(msg)

    try:
        result = subprocess.run(
            [exe, "-p", "Reply with only the word READY.", "-s",
             "--no-custom-instructions", "--no-ask-user"],
            capture_output=True, text=True, timeout=30,
        )
        response = (result.stdout or "").strip().upper()
        ok = "READY" in response
    except subprocess.TimeoutExpired:
        ok = False
        response = "TIMEOUT"
    except FileNotFoundError:
        ok = False
        response = "NOT_FOUND"

    if ok:
        if console:
            console.print("  Copilot authentication confirmed.", style="green")
        else:
            _plain_ok("Copilot authentication confirmed.")
    else:
        msg = (
            "Copilot auth check failed.\n"
            "  Make sure you are signed in to GitHub Copilot:\n"
            "  - Open VS Code and sign in via the Copilot extension, OR\n"
            "  - Run: copilot auth login\n"
            "  Then re-run setup."
        )
        if console:
            console.print(f"  {msg}", style="yellow")
        else:
            _plain_warn(msg)
    return ok


def step_transcripts_folder(console=None) -> str | None:
    prompt_text = (
        "\n  Enter the full path to the folder where transcript files (.vtt, .txt) are stored\n"
        "  (or will be written after transcription):\n  > "
    )
    while True:
        raw = input(prompt_text).strip().strip('"').strip("'")
        if not raw:
            print("  Path cannot be empty.")
            continue
        p = Path(raw)
        if not p.exists():
            print(f"  Folder not found: {p}")
            answer = input("  Create it? (Y/n): ").strip().lower()
            if answer == "n":
                answer2 = input("  Try a different path? (Y/n): ").strip().lower()
                if answer2 == "n":
                    return None
                continue
            p.mkdir(parents=True, exist_ok=True)

        exts = {".vtt", ".txt"}
        found = [f for f in p.rglob("*") if f.suffix.lower() in exts and f.is_file()]
        count = len(found)
        if count == 0:
            note = "  No .vtt or .txt files found yet — that's fine if transcription hasn't run."
            if console:
                console.print(note, style="yellow")
            else:
                print(note)
        else:
            ok_msg = f"  Found {count} transcript file(s)."
            if console:
                console.print(ok_msg, style="green")
            else:
                _plain_ok(ok_msg)

        return str(p)


def step_recordings_folder(console=None) -> tuple[str | None, str | None]:
    """Ask whether MP4s are in a separate folder (e.g. OneDrive). Returns (recordings_path, staging_path)."""
    intro = (
        "\n  Are your MP4 recordings stored in a separate folder from your transcripts?\n"
        "  (e.g. a OneDrive/SharePoint-synced Recordings folder on a low-disk VM)\n"
        "  If yes, files will be copied one at a time to a staging folder, transcribed,\n"
        "  then deleted — so only ~1 GB of local disk is needed at a time."
    )
    if console:
        console.print(intro)
    else:
        print(intro)

    answer = input("\n  Separate recordings folder? (y/N): ").strip().lower()
    if answer != "y":
        return None, None

    prompt_text = "\n  Path to the recordings folder (OneDrive/SharePoint sync):\n  > "
    recordings_path = None
    while True:
        raw = input(prompt_text).strip().strip('"').strip("'")
        if not raw:
            print("  Path cannot be empty.")
            continue
        p = Path(raw)
        if not p.exists():
            print(f"  Folder not found: {p}")
            answer2 = input("  Try again? (Y/n): ").strip().lower()
            if answer2 == "n":
                return None, None
            continue
        mp4s = list(p.rglob("*.mp4"))
        if mp4s:
            ok_msg = f"  Found {len(mp4s)} MP4 file(s)."
            if console:
                console.print(ok_msg, style="green")
            else:
                _plain_ok(ok_msg)
        else:
            note = "  No MP4 files found yet — that's fine if they haven't synced."
            if console:
                console.print(note, style="yellow")
            else:
                print(note)
        recordings_path = str(p)
        break

    default_staging = str(_HERE / "staging")
    raw = input(
        f"\n  Local staging folder (press Enter for default):\n  [{default_staging}]\n  > "
    ).strip().strip('"').strip("'")
    staging_path = raw if raw else default_staging
    Path(staging_path).mkdir(parents=True, exist_ok=True)
    ok_msg = f"  Staging folder: {staging_path}"
    if console:
        console.print(ok_msg, style="green")
    else:
        _plain_ok(ok_msg)

    return recordings_path, staging_path


def step_output_folder(transcripts_path: str, console=None) -> str:
    default = str(Path(transcripts_path).parent / "analyzed_output")
    raw = input(f"\n  Output folder (press Enter for default):\n  [{default}]\n  > ").strip().strip('"').strip("'")
    chosen = raw if raw else default
    Path(chosen).mkdir(parents=True, exist_ok=True)
    ok_msg = f"  Output folder: {chosen}"
    if console:
        console.print(ok_msg, style="green")
    else:
        _plain_ok(ok_msg)
    return chosen


def step_context_files(console=None) -> dict[str, bool]:
    files = {
        "program_brief.txt": "Program_Context_Brief.md from Workcall Drive",
        "rolodex.txt": "04_people_rolodex.md from Workcall Drive",
        "solution_prompt.txt": "SOLUTION code block from PromptLibrary.md",
    }
    status: dict[str, bool] = {}
    all_present = True
    for fname, source in files.items():
        present = (CONTEXT_DIR / fname).exists()
        status[fname] = present
        if present:
            if console:
                console.print(f"  [OK] client_context/{fname}", style="green")
            else:
                _plain_ok(f"client_context/{fname}")
        else:
            all_present = False
            msg = f"  MISSING: client_context/{fname}  (source: {source})"
            if console:
                console.print(msg, style="yellow")
            else:
                _plain_warn(msg)

    if not all_present:
        note = (
            "\n  These files are optional but improve analysis quality.\n"
            f"  Copy them into: {CONTEXT_DIR}\n"
            "  The analyzer will use fallback prompts if they are absent."
        )
        if console:
            console.print(note, style="yellow")
        else:
            print(note)

    return status


def write_env(
    transcripts_path: str,
    output_path: str,
    recordings_path: str | None = None,
    staging_path: str | None = None,
) -> None:
    lines = []

    if ENV_EXAMPLE.exists():
        raw = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        for line in raw:
            stripped = line.strip()
            if stripped.startswith("TRANSCRIPTS_PATH="):
                lines.append(f"TRANSCRIPTS_PATH={transcripts_path}")
            elif stripped.startswith("OUTPUT_PATH=") or stripped.startswith("# OUTPUT_PATH="):
                lines.append(f"OUTPUT_PATH={output_path}")
            elif stripped.startswith("# RECORDINGS_PATH="):
                if recordings_path:
                    lines.append(f"RECORDINGS_PATH={recordings_path}")
                else:
                    lines.append(line)
            elif stripped.startswith("# STAGING_PATH="):
                if staging_path:
                    lines.append(f"STAGING_PATH={staging_path}")
                else:
                    lines.append(line)
            else:
                lines.append(line)
    else:
        lines = [
            "# Generated by setup_wizard.py",
            f"TRANSCRIPTS_PATH={transcripts_path}",
            f"OUTPUT_PATH={output_path}",
        ]
        if recordings_path:
            lines.append(f"RECORDINGS_PATH={recordings_path}")
        if staging_path:
            lines.append(f"STAGING_PATH={staging_path}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_setup_marker(data: dict) -> None:
    SETUP_MARKER.write_text(
        json.dumps({"completed_at": datetime.now(UTC).isoformat(), **data}, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main wizard flow
# ---------------------------------------------------------------------------

def run_wizard(reset: bool = False) -> bool:
    if SETUP_MARKER.exists() and not reset:
        # Already done — caller should check this before invoking
        return True

    _plain_header("Transcript Analyzer — First-time Setup")
    print(
        "\n  This wizard will check your environment, configure your folders,\n"
        "  and write the settings file. It takes about 2 minutes.\n"
        "  You only need to do this once.\n"
    )

    # Step 1: Python version (before rich is available)
    _plain_step(1, TOTAL_STEPS, "Python version")
    if not step_python_version():
        print("\n  Setup cannot continue. Please install Python 3.9 or higher.")
        return False

    # Step 2: Install deps (rich becomes available after this)
    _plain_step(2, TOTAL_STEPS, "Installing dependencies")
    if not step_install_deps():
        print("\n  Setup cannot continue. Fix the pip error above and try again.")
        return False

    # Now we can use rich
    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    def header(text: str) -> None:
        if console:
            console.rule(f"[bold]{text}[/bold]")
        else:
            _plain_header(text)

    def step_label(n: int, text: str) -> None:
        if console:
            console.print(f"\n[bold cyan][{n}/{TOTAL_STEPS}][/bold cyan] {text}")
        else:
            _plain_step(n, TOTAL_STEPS, text)

    # Step 3: ffmpeg
    step_label(3, "ffmpeg")
    ffmpeg_ok = step_ffmpeg(console)

    # Step 4: Copilot CLI
    step_label(4, "GitHub Copilot CLI")
    copilot_ok = step_copilot_cli(console)
    if not copilot_ok:
        print("\n  Setup cannot continue without Copilot CLI. Install it and re-run setup.")
        return False

    # Step 5: Copilot auth
    step_label(5, "Copilot authentication")
    auth_ok = step_copilot_auth(console)
    if not auth_ok:
        print("\n  Warning: Copilot auth check failed. You can continue setup but")
        print("  analysis runs will fail until Copilot is authenticated.")
        answer = input("  Continue anyway? (Y/n): ").strip().lower()
        if answer == "n":
            return False

    # Step 6: Transcripts folder
    step_label(6, "Transcripts folder")
    transcripts_path = step_transcripts_folder(console)
    if not transcripts_path:
        print("\n  Setup cancelled — no transcripts folder selected.")
        return False

    # Step 7: Recordings folder (optional — OneDrive/low-disk scenario)
    step_label(7, "Recordings folder (MP4 source)")
    recordings_path, staging_path = step_recordings_folder(console)

    # Step 8: Output folder
    step_label(8, "Output folder")
    output_path = step_output_folder(transcripts_path, console)

    # Step 9: Context files
    step_label(9, "Optional context files")
    context_status = step_context_files(console)

    # Write config
    write_env(transcripts_path, output_path, recordings_path, staging_path)
    write_setup_marker({
        "transcripts_path": transcripts_path,
        "output_path": output_path,
        "recordings_path": recordings_path,
        "staging_path": staging_path,
        "ffmpeg_available": ffmpeg_ok,
        "copilot_auth_ok": auth_ok,
        "context_files": context_status,
    })

    if console:
        console.print("\n[bold green]Setup complete![/bold green]")
        console.print(f"  Settings written to: {ENV_PATH}")
        console.print(f"  Run the analyzer:    [bold]python run.py[/bold]")
    else:
        print("\n  Setup complete!")
        print(f"  Settings written to: {ENV_PATH}")
        print("  Run the analyzer:    python run.py")

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="First-time setup wizard for the transcript analyzer")
    parser.add_argument("--reset", action="store_true", help="Re-run setup even if already configured")
    args = parser.parse_args()

    success = run_wizard(reset=args.reset)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
