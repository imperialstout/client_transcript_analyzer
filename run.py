"""
run.py — daily driver for the transcript analyzer.

Normal use: python run.py
  - Checks setup is complete (routes to setup_wizard.py if not)
  - Scans for new or changed transcripts
  - Runs analysis and summaries via analyze_copilot.py
  - Generates a semantic changelog comparing old vs new summaries
  - Prints a status report

Optional flags:
  --setup         Re-run the setup wizard
  --qc-only       Transcript quality check only (no API calls)
  --summary-only  Re-run summaries from existing analyses only
  --no-changelog  Skip changelog generation
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).parent
_PROJECT_ENV = _HERE / ".env"
_LEGACY_ENV = Path.home() / ".config" / "client-transcript-analyzer" / ".env"
SETUP_MARKER = _HERE / "setup_complete.json"

load_dotenv(_PROJECT_ENV)
if not _PROJECT_ENV.exists():
    load_dotenv(_LEGACY_ENV)

TRANSCRIPTS_PATH_RAW = os.environ.get("TRANSCRIPTS_PATH", "").strip()
TRANSCRIPTS_PATH = Path(TRANSCRIPTS_PATH_RAW) if TRANSCRIPTS_PATH_RAW else None
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", _HERE / "output"))
COPILOT_TIMEOUT_SECONDS = int(os.environ.get("COPILOT_TIMEOUT_SECONDS", "900"))
MODEL_CHANGELOG = os.environ.get("COPILOT_MODEL_SUMMARY", "auto")


# ---------------------------------------------------------------------------
# Rich setup
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    console = Console()
except ImportError:
    console = None


def _print(msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style)
    else:
        print(msg)


def _rule(title: str = "") -> None:
    if console:
        console.rule(f"[bold]{title}[/bold]" if title else "")
    else:
        print(f"\n{'=' * 60}" + (f"\n  {title}" if title else ""))


# ---------------------------------------------------------------------------
# Copilot CLI (mirrors analyze_copilot.py — kept local to avoid import side-effects)
# ---------------------------------------------------------------------------

def _copilot_executable() -> str:
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
    return cp if cp else "copilot"


def _run_copilot_changelog(old_text: str, new_text: str, label: str) -> str:
    import tempfile

    exe = _copilot_executable()
    prompt = (
        "You are tracking design evolution across meeting transcripts. "
        "Compare the two summaries below and describe what changed.\n\n"
        "Focus on:\n"
        "- Decisions that moved from open to confirmed (or reversed)\n"
        "- New requirements or constraints surfaced\n"
        "- Open items that were resolved or newly raised\n"
        "- Key themes that strengthened, weakened, or disappeared\n\n"
        "Be specific and concise. If nothing meaningful changed, say so in one sentence.\n\n"
        f"SECTION: {label}\n\n"
        "BEFORE:\n" + old_text + "\n\n"
        "AFTER:\n" + new_text
    )

    with tempfile.TemporaryDirectory(prefix="copilot_changelog_") as tmp_dir:
        stdout_path = Path(tmp_dir) / "stdout.txt"
        stderr_path = Path(tmp_dir) / "stderr.txt"
        cmd = [
            exe, "-p", prompt, "-s",
            "--no-custom-instructions", "--no-ask-user",
        ]
        if MODEL_CHANGELOG and MODEL_CHANGELOG != "auto":
            cmd.extend(["--model", MODEL_CHANGELOG])
        try:
            with open(stdout_path, "w", encoding="utf-8") as out_f, \
                 open(stderr_path, "w", encoding="utf-8") as err_f:
                subprocess.run(
                    cmd, check=True, stdout=out_f, stderr=err_f,
                    text=True, timeout=COPILOT_TIMEOUT_SECONDS,
                )
            return stdout_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as exc:
            return f"[changelog generation failed: {exc}]"


# ---------------------------------------------------------------------------
# Transcript scan
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _discover_transcripts(root: Path) -> list[Path]:
    exts = {".vtt", ".txt"}
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def _bu_for(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return "ROOT"
    return rel.parts[0] if len(rel.parts) > 1 else "ROOT"


def _meta_path(transcript: Path, bu: str) -> Path:
    return OUTPUT_PATH / bu / f"{transcript.stem} [ANALYZED].meta.json"


def _read_meta(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def scan_transcripts(root: Path) -> tuple[list[Path], list[Path]]:
    """Return (new_or_changed, already_done) based on meta sidecar freshness."""
    new_or_changed: list[Path] = []
    already_done: list[Path] = []

    for t in _discover_transcripts(root):
        bu = _bu_for(t, root)
        meta = _read_meta(_meta_path(t, bu))
        if meta and meta.get("status") == "ok":
            stat = t.stat()
            if (
                meta.get("source_sha256") == _sha256_file(t)
                and meta.get("source_size") == stat.st_size
                and meta.get("source_mtime_ns") == stat.st_mtime_ns
            ):
                already_done.append(t)
                continue
        new_or_changed.append(t)

    return new_or_changed, already_done


# ---------------------------------------------------------------------------
# Summary snapshots (for changelog diff)
# ---------------------------------------------------------------------------

def _collect_summaries(output_root: Path) -> dict[str, str]:
    """Collect all existing BU/feature summary .md files into {label: content}."""
    summaries: dict[str, str] = {}
    for md in output_root.rglob("*.md"):
        name = md.name
        if name.startswith("[BU SUMMARY]") or name.startswith("[FEATURE SUMMARY]"):
            label = md.stem.replace("[BU SUMMARY] ", "").replace("[FEATURE SUMMARY] ", "")
            summaries[label] = md.read_text(encoding="utf-8", errors="replace")
    return summaries


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Changelog writer
# ---------------------------------------------------------------------------

def generate_changelog(
    before: dict[str, str],
    after: dict[str, str],
    processed_count: int,
    failed_count: int,
    run_label: str,
) -> Path:
    changelog_dir = OUTPUT_PATH / "changelog"
    changelog_dir.mkdir(parents=True, exist_ok=True)
    out_path = changelog_dir / f"[CHANGELOG] {run_label}.md"

    changed_labels = [
        label for label, content in after.items()
        if _md5(content) != _md5(before.get(label, ""))
    ]
    new_labels = [label for label in after if label not in before]

    lines: list[str] = [
        f"# Changelog — {run_label}",
        "",
        "## What ran",
        f"- {processed_count} transcript(s) analyzed",
    ]

    if changed_labels:
        updated = [l for l in changed_labels if l not in new_labels]
        if updated:
            lines.append(f"- {len(updated)} summary section(s) updated: {', '.join(updated)}")
    if new_labels:
        lines.append(f"- {len(new_labels)} new section(s) added: {', '.join(new_labels)}")
    if failed_count:
        lines.append(f"- {failed_count} transcript(s) failed or timed out (see NEEDS_FOLLOWUP.txt)")

    lines += ["", "## What shifted", ""]

    if not changed_labels:
        lines.append("No summary content changed in this run.")
    else:
        for label in sorted(changed_labels):
            lines.append(f"### {label}")
            old = before.get(label, "")
            new = after.get(label, "")
            if not old:
                lines.append("_(New section — no prior content to compare against.)_")
            else:
                _print(f"  [changelog] diffing '{label}' ...")
                diff_text = _run_copilot_changelog(old, new, label)
                lines.append(diff_text)
            lines.append("")

    # Needs-followup section
    nf_path = OUTPUT_PATH / "NEEDS_FOLLOWUP.txt"
    if nf_path.exists():
        nf_lines = nf_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if nf_lines:
            lines += ["", "## Needs follow-up", ""]
            for entry in nf_lines[-20:]:  # cap at last 20 to avoid huge changelogs
                lines.append(f"- {entry}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Run the pipeline via subprocess
# ---------------------------------------------------------------------------

def _run_script(args_list: list[str]) -> int:
    cmd = [sys.executable, str(_HERE / "analyze_copilot.py")] + args_list
    result = subprocess.run(cmd)
    return result.returncode


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------

def _print_status_report(
    new_count: int,
    done_count: int,
    failed_count: int,
    changelog_path: Path | None,
    log_path: Path | None,
) -> None:
    _rule("Run complete")

    if console:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column("Label", style="bold")
        table.add_column("Value")
        table.add_row("Transcripts processed", str(new_count))
        table.add_row("Transcripts skipped", str(done_count))
        table.add_row("Failed / timed out", str(failed_count) if failed_count else "none")
        if changelog_path:
            table.add_row("Changelog written", str(changelog_path))
        if log_path:
            table.add_row("Run log", str(log_path))
        console.print(table)
    else:
        print(f"  Transcripts processed : {new_count}")
        print(f"  Transcripts skipped   : {done_count}")
        print(f"  Failed / timed out    : {failed_count if failed_count else 'none'}")
        if changelog_path:
            print(f"  Changelog written     : {changelog_path}")
        if log_path:
            print(f"  Run log               : {log_path}")

    nf_path = OUTPUT_PATH / "NEEDS_FOLLOWUP.txt"
    if nf_path.exists():
        nf_lines = nf_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if nf_lines:
            _print("\n[bold yellow]NEEDS FOLLOW-UP[/bold yellow]" if console else "\nNEEDS FOLLOW-UP:")
            for line in nf_lines:
                _print(f"  {line}", style="yellow")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Transcript analyzer — daily driver")
    parser.add_argument("--setup", action="store_true", help="Re-run the setup wizard")
    parser.add_argument("--qc-only", action="store_true", help="Quality check only, no API calls")
    parser.add_argument("--summary-only", action="store_true", help="Re-run summaries from existing analyses")
    parser.add_argument("--no-changelog", action="store_true", help="Skip changelog generation")
    args = parser.parse_args()

    # Setup
    if args.setup or not SETUP_MARKER.exists():
        _print("[bold]First-time setup required. Launching wizard...[/bold]" if console else "First-time setup required. Launching wizard...")
        result = subprocess.run([sys.executable, str(_HERE / "setup_wizard.py")])
        if result.returncode != 0:
            sys.exit("Setup did not complete. Run 'python setup_wizard.py' to try again.")
        # Reload env after setup
        load_dotenv(_PROJECT_ENV, override=True)
        if args.setup:
            return

    if not TRANSCRIPTS_PATH_RAW or not TRANSCRIPTS_PATH or not TRANSCRIPTS_PATH.exists():
        sys.exit(
            f"TRANSCRIPTS_PATH is missing or not found: '{TRANSCRIPTS_PATH_RAW}'\n"
            "Run 'python run.py --setup' to reconfigure."
        )

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)

    _rule("Transcript Analyzer")

    # QC-only shortcut
    if args.qc_only:
        _print("Running quality check only ...")
        _run_script(["--qc-only"])
        return

    # Summary-only shortcut
    if args.summary_only:
        _print("Re-running summaries from existing analyses ...")
        before = _collect_summaries(OUTPUT_PATH)
        _run_script(["--summary-only"])
        if not args.no_changelog:
            after = _collect_summaries(OUTPUT_PATH)
            run_label = datetime.now(UTC).strftime("%Y-%m-%d %H%M")
            changelog_path = generate_changelog(before, after, 0, 0, run_label)
            _print(f"Changelog: {changelog_path}")
        return

    # Normal run
    new_transcripts, done_transcripts = scan_transcripts(TRANSCRIPTS_PATH)
    total = len(new_transcripts) + len(done_transcripts)

    if console:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column("", style="bold")
        table.add_column("")
        table.add_row("Total transcripts found", str(total))
        table.add_row("Already analyzed", str(len(done_transcripts)))
        table.add_row("New or changed", str(len(new_transcripts)))
        console.print(table)
    else:
        print(f"  Total transcripts : {total}")
        print(f"  Already analyzed  : {len(done_transcripts)}")
        print(f"  New or changed    : {len(new_transcripts)}")

    if not new_transcripts:
        _print("\nNothing to do — all transcripts are already analyzed.", style="green" if console else "")
        answer = input("Re-run summaries anyway? (y/N): ").strip().lower()
        if answer != "y":
            return
        before = _collect_summaries(OUTPUT_PATH)
        _run_script(["--summary-only"])
        if not args.no_changelog:
            after = _collect_summaries(OUTPUT_PATH)
            run_label = datetime.now(UTC).strftime("%Y-%m-%d %H%M")
            generate_changelog(before, after, 0, 0, run_label)
        return

    answer = input(f"\nAnalyze {len(new_transcripts)} transcript(s) and update summaries? (Y/n): ").strip().lower()
    if answer == "n":
        _print("Cancelled.")
        return

    # Snapshot summaries before run
    before = _collect_summaries(OUTPUT_PATH)

    # Phase 1: transcript analysis
    _rule("Analyzing transcripts")
    rc1 = _run_script(["--transcript-only"])

    # Phase 2: summaries
    _rule("Updating summaries")
    rc2 = _run_script(["--summary-only"])

    # Count failures from NEEDS_FOLLOWUP.txt entries added this run
    nf_path = OUTPUT_PATH / "NEEDS_FOLLOWUP.txt"
    failed_count = 0
    if nf_path.exists():
        failed_count = len(nf_path.read_text(encoding="utf-8").strip().splitlines())

    # Find most recent log
    log_path = None
    logs_dir = OUTPUT_PATH / "logs"
    if logs_dir.exists():
        run_logs = sorted(logs_dir.glob("run_*.log"), reverse=True)
        if run_logs:
            log_path = run_logs[0]

    # Changelog
    changelog_path = None
    if not args.no_changelog:
        _rule("Generating changelog")
        after = _collect_summaries(OUTPUT_PATH)
        run_label = datetime.now(UTC).strftime("%Y-%m-%d %H%M")
        changelog_path = generate_changelog(
            before, after,
            processed_count=len(new_transcripts),
            failed_count=failed_count,
            run_label=run_label,
        )

    _print_status_report(
        new_count=len(new_transcripts),
        done_count=len(done_transcripts),
        failed_count=failed_count,
        changelog_path=changelog_path,
        log_path=log_path,
    )


if __name__ == "__main__":
    main()
