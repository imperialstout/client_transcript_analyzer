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
# Long-path-safe file I/O (mirrors analyze_copilot.py)
#
# BU folder names + analysis filenames can push the full path past Windows'
# legacy 260-char MAX_PATH limit, especially under deeply nested OneDrive
# paths. Use the \\?\ extended-length prefix so reads/writes here can't
# raise FileNotFoundError and crash this script mid-run.
# ---------------------------------------------------------------------------

def _long_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    resolved = str(path.resolve())
    if resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):  # UNC path
        return "\\\\?\\UNC\\" + resolved.lstrip("\\")
    return "\\\\?\\" + resolved


def _safe_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    os.makedirs(_long_path(path.parent), exist_ok=True)
    with open(_long_path(path), "w", encoding=encoding) as f:
        f.write(content)


def _safe_read_text(path: Path, encoding: str = "utf-8", errors: str = "strict") -> str:
    with open(_long_path(path), "r", encoding=encoding, errors=errors) as f:
        return f.read()


def _safe_read_bytes(path: Path) -> bytes:
    with open(_long_path(path), "rb") as f:
        return f.read()


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
    return hashlib.sha256(_safe_read_bytes(path)).hexdigest()


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
        return json.loads(_safe_read_text(path, encoding="utf-8"))
    except Exception:
        return None


def scan_transcripts(root: Path) -> tuple[list[Path], list[Path]]:
    """Return (new_or_changed, already_done) based on meta sidecar freshness."""
    new_or_changed: list[Path] = []
    already_done: list[Path] = []

    for t in _discover_transcripts(root):
        try:
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
        except Exception as e:
            # A single unreadable/long-path transcript should not abort the
            # whole scan; treat it as new/changed so analyze_copilot.py
            # (which has its own long-path-safe I/O) gets a chance at it.
            _print(f"  [warn] could not check cache status for '{t.name}': {e}")
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
            try:
                summaries[label] = _safe_read_text(md, encoding="utf-8", errors="replace")
            except Exception as e:
                _print(f"  [warn] could not read summary '{md.name}': {e}")
    return summaries


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# NEEDS_FOLLOWUP.txt helpers (isolate issues from this run only)
# ---------------------------------------------------------------------------

def _read_followup_lines() -> set[str]:
    nf_path = OUTPUT_PATH / "NEEDS_FOLLOWUP.txt"
    if not nf_path.exists():
        return set()
    try:
        return set(_safe_read_text(nf_path, encoding="utf-8", errors="replace").strip().splitlines())
    except Exception:
        return set()


def _new_followup_lines(before: set[str]) -> list[str]:
    return sorted(_read_followup_lines() - before)


# ---------------------------------------------------------------------------
# Context review suggestions
# ---------------------------------------------------------------------------

def _generate_context_review(new_transcripts: list[Path]) -> None:
    """
    After analysis, scan the new [ANALYZED].txt files for names and facts
    that don't appear in the context files. Write a suggestion file for
    human review — never modifies context files automatically.
    """
    if not new_transcripts:
        return

    context_dir = _HERE / "client_context"
    rolodex_path = context_dir / "rolodex.txt"
    brief_path = context_dir / "program_brief.txt"

    rolodex_text = rolodex_path.read_text(encoding="utf-8", errors="replace") if rolodex_path.exists() else ""
    brief_text = brief_path.read_text(encoding="utf-8", errors="replace") if brief_path.exists() else ""
    known_context = (rolodex_text + "\n" + brief_text).lower()

    # Collect the analyzed output for the new transcripts
    analyzed_texts: list[str] = []
    for t in new_transcripts:
        bu = _bu_for(t, TRANSCRIPTS_PATH)
        analyzed = OUTPUT_PATH / bu / f"{t.stem} [ANALYZED].txt"
        if analyzed.exists():
            try:
                analyzed_texts.append(f"--- {t.name} ---\n{_safe_read_text(analyzed, encoding='utf-8', errors='replace')}")
            except Exception as e:
                _print(f"  [warn] could not read analysis for '{t.name}': {e}")

    if not analyzed_texts:
        return

    bundle = "\n\n".join(analyzed_texts)

    prompt = (
        "You are a program context reviewer. Read the analyzed transcript excerpts below.\n\n"
        "Identify any of the following that appear in the transcripts but are NOT present in the "
        "known context (rolodex and program brief):\n"
        "1. Person names — new stakeholders, attendees, or roles mentioned\n"
        "2. System or product names not in the brief\n"
        "3. Organizational units or team names not in the brief\n"
        "4. Key constraints or decisions that seem program-wide and should be in the brief\n\n"
        "Format your response as a short markdown list under these headings:\n"
        "## Possible new people\n## Possible new systems/products\n## Possible new org names\n## Possible brief updates\n\n"
        "If nothing is missing, say so under each heading. Be concise.\n\n"
        f"KNOWN CONTEXT (rolodex + brief):\n{known_context[:3000]}\n\n"
        f"ANALYZED TRANSCRIPTS:\n{bundle[:8000]}"
    )

    _print("  [context review] scanning for missing context ...")
    exe = _copilot_executable()
    import tempfile
    with tempfile.TemporaryDirectory(prefix="copilot_ctx_") as tmp_dir:
        stdout_path = Path(tmp_dir) / "stdout.txt"
        stderr_path = Path(tmp_dir) / "stderr.txt"
        cmd = [exe, "-p", prompt, "-s", "--no-custom-instructions", "--no-ask-user"]
        try:
            with open(stdout_path, "w", encoding="utf-8") as out_f, \
                 open(stderr_path, "w", encoding="utf-8") as err_f:
                subprocess.run(cmd, check=True, stdout=out_f, stderr=err_f,
                               text=True, timeout=COPILOT_TIMEOUT_SECONDS)
            suggestion = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as exc:
            suggestion = f"[context review failed: {exc}]"

    run_label = datetime.now(UTC).strftime("%Y-%m-%d %H%M")
    out_path = OUTPUT_PATH / f"[SUGGESTED UPDATES] context_review {run_label}.md"
    _safe_write_text(
        out_path,
        f"# Suggested Context Updates — {run_label}\n\n"
        "These are items found in recent transcripts that do not appear in your\n"
        "rolodex or program brief. **Review and update those files manually** if relevant.\n"
        "This file is generated automatically and safe to delete after review.\n\n"
        + suggestion + "\n",
        encoding="utf-8",
    )
    _print(f"  [context review] suggestions written -> {out_path.name}")


# ---------------------------------------------------------------------------
# Changelog writer
# ---------------------------------------------------------------------------

def generate_changelog(
    before: dict[str, str],
    after: dict[str, str],
    processed_files: list[Path],
    this_run_issues: list[str],
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
        f"- {len(processed_files)} transcript(s) analyzed",
    ]

    if processed_files:
        for f in processed_files:
            lines.append(f"  - {f.name}")

    if changed_labels:
        updated = [l for l in changed_labels if l not in new_labels]
        if updated:
            lines.append(f"- {len(updated)} summary section(s) updated: {', '.join(updated)}")
    if new_labels:
        lines.append(f"- {len(new_labels)} new section(s) added: {', '.join(new_labels)}")

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

    # Issues this run — always present so readers never have to wonder
    lines += ["## Issues this run", ""]
    if this_run_issues:
        for entry in this_run_issues:
            lines.append(f"- {entry}")
    else:
        lines.append("No errors or timeouts.")

    _safe_write_text(out_path, "\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Run the pipeline via subprocess
# ---------------------------------------------------------------------------

def _run_script(args_list: list[str]) -> int:
    cmd = [sys.executable, str(_HERE / "analyze_copilot.py")] + args_list
    result = subprocess.run(cmd)
    return result.returncode


def _run_script_or_die(args_list: list[str], phase_label: str) -> None:
    """Run analyze_copilot.py and stop the whole run.py process immediately
    if it fails, instead of silently continuing to the next phase and
    printing a "Run complete" report that would misrepresent what actually
    happened. A non-technical user needs an unmissable, plain-English signal
    here — not a status table that looks fine because nothing had the
    chance to log a per-file failure."""
    returncode = _run_script(args_list)
    if returncode != 0:
        _print(
            f"\n[bold red]STOPPED: the '{phase_label}' step failed (exit code {returncode}).[/bold red]"
            if console else
            f"\nSTOPPED: the '{phase_label}' step failed (exit code {returncode})."
        )
        _print(
            "Nothing further will run. Scroll up for the error from analyze_copilot.py, "
            "or check the most recent log in the output folder's 'logs' subfolder.\n"
            "Common causes: not signed in to GitHub Copilot CLI (or signed in with the wrong "
            "account), no network connection, or TRANSCRIPTS_PATH/OUTPUT_PATH misconfigured."
        )
        sys.exit(returncode)


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

    # Find most recent context review suggestion file
    context_review = None
    review_files = sorted(OUTPUT_PATH.glob("[SUGGESTED UPDATES] context_review *.md"), reverse=True)
    if review_files:
        context_review = review_files[0]

    if console:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        table.add_column("Label", style="bold")
        table.add_column("Value")
        table.add_row("Transcripts processed", str(new_count))
        table.add_row("Transcripts skipped", str(done_count))
        table.add_row("Failed / timed out", str(failed_count) if failed_count else "none")
        if changelog_path:
            table.add_row("Changelog", str(changelog_path))
        if context_review:
            table.add_row("Context review", str(context_review))
        if log_path:
            table.add_row("Run log", str(log_path))
        console.print(table)
    else:
        print(f"  Transcripts processed : {new_count}")
        print(f"  Transcripts skipped   : {done_count}")
        print(f"  Failed / timed out    : {failed_count if failed_count else 'none'}")
        if changelog_path:
            print(f"  Changelog             : {changelog_path}")
        if context_review:
            print(f"  Context review        : {context_review}")
        if log_path:
            print(f"  Run log               : {log_path}")

    if failed_count:
        _print(
            "\n[bold yellow]Some transcripts failed or timed out — see 'Issues this run' in the changelog.[/bold yellow]"
            if console else
            "\nSome transcripts failed or timed out — see 'Issues this run' in the changelog."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Transcript analyzer — daily driver")
    parser.add_argument("--setup", action="store_true", help="Re-run the setup wizard")
    parser.add_argument("--qc-only", action="store_true", help="Quality check only, no API calls")
    parser.add_argument("--summary-only", action="store_true", help="Re-run summaries from existing analyses")
    parser.add_argument("--no-changelog", action="store_true", help="Skip changelog generation")
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Auto-confirm all prompts (required for non-interactive/background/detached runs)",
    )
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
        nf_before = _read_followup_lines()
        _run_script_or_die(["--summary-only"], "summary regeneration")
        if not args.no_changelog:
            after = _collect_summaries(OUTPUT_PATH)
            run_label = datetime.now(UTC).strftime("%Y-%m-%d %H%M")
            changelog_path = generate_changelog(
                before, after, [], _new_followup_lines(nf_before), run_label
            )
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
        if args.yes:
            answer = "y"
        else:
            answer = input("Re-run summaries anyway? (y/N): ").strip().lower()
        if answer != "y":
            return
        before = _collect_summaries(OUTPUT_PATH)
        nf_before = _read_followup_lines()
        _run_script_or_die(["--summary-only"], "summary regeneration")
        if not args.no_changelog:
            after = _collect_summaries(OUTPUT_PATH)
            run_label = datetime.now(UTC).strftime("%Y-%m-%d %H%M")
            generate_changelog(before, after, [], _new_followup_lines(nf_before), run_label)
        return

    if args.yes:
        answer = "y"
    else:
        answer = input(f"\nAnalyze {len(new_transcripts)} transcript(s) and update summaries? (Y/n): ").strip().lower()
    if answer == "n":
        _print("Cancelled.")
        return

    # Snapshot state before run
    before = _collect_summaries(OUTPUT_PATH)
    nf_before = _read_followup_lines()

    # Phase 1: transcript analysis
    _rule("Analyzing transcripts")
    _run_script_or_die(["--transcript-only"], "transcript analysis")

    # Phase 2: summaries
    _rule("Updating summaries")
    _run_script_or_die(["--summary-only"], "summary generation")

    this_run_issues = _new_followup_lines(nf_before)

    # Context review suggestions
    _generate_context_review(new_transcripts)

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
            processed_files=new_transcripts,
            this_run_issues=this_run_issues,
            run_label=run_label,
        )

    _print_status_report(
        new_count=len(new_transcripts),
        done_count=len(done_transcripts),
        failed_count=len(this_run_issues),
        changelog_path=changelog_path,
        log_path=log_path,
    )


if __name__ == "__main__":
    main()
