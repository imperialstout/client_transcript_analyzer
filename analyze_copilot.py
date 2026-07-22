"""
Client transcript analysis tool — BU pipeline and UC/gap-report pipeline.

BU PIPELINE (default):
    Walks TRANSCRIPTS_PATH for .vtt/.txt files by subfolder (= BU name),
    analyzes each transcript with Copilot CLI, and produces per-BU summaries.

    python analyze_copilot.py                  # all BUs
    python analyze_copilot.py --bu "ARM"       # single BU
    python analyze_copilot.py --transcript-only
    python analyze_copilot.py --summary-only
    python analyze_copilot.py --qc-only
    python analyze_copilot.py --qc-threshold 70

UC PIPELINE:
    Maps transcript folders to the three backlog CSVs by numeric prefix,
    analyzes each transcript with feature-context injected, produces feature
    summaries, UC rollups, and a gap report showing which backlog items have
    no transcript coverage.

    python analyze_copilot.py --uc             # full UC pipeline + gap report
    python analyze_copilot.py --uc --feature 1.04  # single feature
    python analyze_copilot.py --uc --summary-only  # re-run summaries only
    python analyze_copilot.py --uc --gap-only      # gap report only (no API calls)
    python analyze_copilot.py --uc --qc-only       # quality check only
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from dotenv import load_dotenv

_HERE = Path(__file__).parent
_PROJECT_ENV = _HERE / ".env"
_LEGACY_ENV = Path.home() / ".config" / "client-transcript-analyzer" / ".env"

load_dotenv(_PROJECT_ENV)
if not _PROJECT_ENV.exists():
    load_dotenv(_LEGACY_ENV)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TRANSCRIPTS_PATH_RAW = os.environ.get("TRANSCRIPTS_PATH", "").strip()
TRANSCRIPTS_PATH = Path(TRANSCRIPTS_PATH_RAW) if TRANSCRIPTS_PATH_RAW else Path()
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", Path(__file__).parent / "output"))
CONTEXT_DIR = Path(__file__).parent / "client_context"

# Use Copilot model IDs when provided; "auto" lets CLI choose.
MODEL_TRANSCRIPT = os.environ.get("COPILOT_MODEL_TRANSCRIPT", "auto")
MODEL_SUMMARY = os.environ.get("COPILOT_MODEL_SUMMARY", "auto")

# Seconds to sleep between API calls to stay under rate limits
RATE_LIMIT_SLEEP = float(os.environ.get("RATE_LIMIT_SLEEP", "2"))
QC_THRESHOLD_DEFAULT = int(os.environ.get("QC_THRESHOLD", "0"))

# Hard ceiling per Copilot CLI call. Most transcripts finish in a few
# minutes; an occasional one may legitimately run long, but a call that
# never returns should not stall the whole batch. Anything that times out
# gets logged to NEEDS_FOLLOWUP.txt and skipped so the run keeps moving.
COPILOT_TIMEOUT_SECONDS = int(os.environ.get("COPILOT_TIMEOUT_SECONDS", "900"))

# How often to print a "still working" heartbeat while waiting on a single
# Copilot CLI call. A non-technical user watching a plain terminal (no
# progress bar, possibly no VS Code) has no way to tell a slow-but-normal
# call apart from a silent hang unless something is printed periodically.
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "30"))

# Simple overall progress counter (e.g. "[12/75]") shown next to each
# transcript line so a non-technical user can see the run is actually
# advancing through a long batch, not just individual per-file activity.
_progress = {"current": 0, "total": 0}


def _progress_tag() -> str:
    if not _progress["total"]:
        return ""
    _progress["current"] += 1
    return f"[{_progress['current']}/{_progress['total']}] "

# ---------------------------------------------------------------------------
# Long-path-safe file I/O
#
# BU folder names + analysis filenames (which embed the source transcript
# name plus " [ANALYZED].txt"/".meta.json") can push the full path past
# Windows' legacy 260-character MAX_PATH limit, especially under deeply
# nested OneDrive paths. That previously caused a write to fail; if it then
# ALSO failed while trying to record the error (same long-path problem),
# the exception went unhandled and crashed the entire batch run instead of
# just that one transcript. These helpers use the \\?\ extended-length
# path prefix (Windows-only; no-op elsewhere) so long paths write/read
# successfully instead of raising FileNotFoundError.
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


def _safe_append_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    os.makedirs(_long_path(path.parent), exist_ok=True)
    with open(_long_path(path), "a", encoding=encoding) as f:
        f.write(content)


# ---------------------------------------------------------------------------
# UC pipeline config
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
CSV_UC0 = _HERE / "client_data" / "20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - Epic UC0.csv"
CSV_UC1 = _HERE / "client_data" / "20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - Epic UC 1.csv"
CSV_UC2 = _HERE / "client_data" / "20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - EpicUC02 Technician Quote.csv"

UC_NAMES = {
    "UC0": "BX SaaS Quote (Use Case 0)",
    "UC1": "SOLSYS End-to-End Quote (Use Case 1)",
    "UC2": "SNGX Technician Quote (Use Case 2)",
}

FEATURES_OUT = OUTPUT_PATH / "features"


def required_csv_paths() -> list[Path]:
    return [CSV_UC0, CSV_UC1, CSV_UC2]


# ---------------------------------------------------------------------------
# UC data model
# ---------------------------------------------------------------------------

@dataclass
class Feature:
    prefix: str
    arm_name: str
    siemens_name: str
    definition: str
    uc0_scope: str
    uc1_scope: str
    uc2_scope: str
    sf_owner: str = ""
    folder: "Path | None" = None

    @property
    def display_name(self) -> str:
        return self.siemens_name if self.siemens_name and self.siemens_name.upper() != "NA" else self.arm_name

    def in_scope(self, uc: str) -> bool:
        val = {"UC0": self.uc0_scope, "UC1": self.uc1_scope, "UC2": self.uc2_scope}.get(uc, "")
        return bool(val) and val.upper() not in ("NO", "NOT APPLICABLE", "N/A", "")

    def scope_label(self, uc: str) -> str:
        val = {"UC0": self.uc0_scope, "UC1": self.uc1_scope, "UC2": self.uc2_scope}.get(uc, "")
        if not val or val.upper() in ("NO", "NOT APPLICABLE", "N/A"):
            return "x"
        if val.upper() == "YES":
            return "Y"
        if "UNCLEAR" in val.upper():
            return "?"
        return "~"


# ---------------------------------------------------------------------------
# Feature registry (CSV parsing)
# ---------------------------------------------------------------------------

_PREFIX_RE = re.compile(r"^(\d+\.\d+)")


def _extract_prefix(name: str) -> str:
    m = _PREFIX_RE.match(name.strip())
    return m.group(1) if m else ""


def load_feature_registry() -> dict[str, Feature]:
    registry: dict[str, Feature] = {}

    def _key(arm_name: str, siemens_name: str) -> str:
        p = _extract_prefix(siemens_name)
        if p:
            return p
        return re.sub(r"[^a-z0-9]+", "-", arm_name.lower().strip()).strip("-")

    def _parse(csv_path: Path, uc_col: str, uc_attr: str) -> None:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                arm = row.get("Epic / Capability Name(ARM)", "").strip()
                siemens = row.get("Epic / Capability Name(Siemens)", "").strip()
                definition = row.get("Definition", "").strip()
                scope_val = (row.get(uc_col, "") or "").strip()
                owner = row.get("SF Owner", "").strip()
                if not arm and not siemens:
                    continue
                key = _key(arm, siemens)
                if key not in registry:
                    registry[key] = Feature(
                        prefix=_extract_prefix(siemens),
                        arm_name=arm,
                        siemens_name=siemens,
                        definition=definition,
                        uc0_scope="", uc1_scope="", uc2_scope="",
                        sf_owner=owner,
                    )
                else:
                    if not registry[key].definition and definition:
                        registry[key].definition = definition
                    if not registry[key].sf_owner and owner:
                        registry[key].sf_owner = owner
                setattr(registry[key], uc_attr, scope_val)

    _parse(CSV_UC0, "UC-00", "uc0_scope")
    _parse(CSV_UC1, "UC-01 SOLSYS E2E Quote", "uc1_scope")
    _parse(CSV_UC2, "UC-02 SNGX Technician", "uc2_scope")
    return registry


def match_folders_to_features(registry: dict[str, Feature]) -> list[Path]:
    """Match TRANSCRIPTS_PATH subdirs to features by numeric prefix. Returns unmatched folders."""
    unmatched: list[Path] = []
    for folder in sorted(TRANSCRIPTS_PATH.iterdir()):
        if not folder.is_dir():
            continue
        prefix = _extract_prefix(folder.name)
        if prefix and prefix in registry:
            registry[prefix].folder = folder
        elif prefix:
            matched = False
            for feat in registry.values():
                if feat.prefix == prefix:
                    feat.folder = folder
                    matched = True
                    break
            if not matched:
                unmatched.append(folder)
        else:
            unmatched.append(folder)
    return unmatched

# ---------------------------------------------------------------------------
# Copilot CLI client
# ---------------------------------------------------------------------------

def _copilot_executable() -> str:
    candidates: list[str] = []
    if os.name == "nt":
        app_data = os.environ.get("APPDATA", "")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        user_home = str(Path.home())
        candidates.extend([
            os.path.join(local_app_data, "Microsoft", "WinGet", "Packages", "GitHub.Copilot_Microsoft.Winget.Source_8wekyb3d8bbwe", "copilot.exe"),
            os.path.join(user_home, "AppData", "Local", "Microsoft", "WinGet", "Links", "copilot.exe"),
            os.path.join(app_data, "Code", "User", "globalStorage", "github.copilot-chat", "copilotCli", "copilot"),
            os.path.join(app_data, "Code", "User", "globalStorage", "github.copilot-chat", "copilotCli", "copilot.bat"),
        ])

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate

    cp_path = shutil.which("copilot")
    if cp_path:
        return cp_path

    return "copilot"


def _run_copilot(
    prompt: str,
    model: str,
    attachments: list[Path] | None = None,
    allow_file_read: bool = False,
) -> str:
    cmd = [
        _copilot_executable(),
        "-p",
        prompt,
        "-s",
        "--no-custom-instructions",
        "--no-ask-user",
    ]
    if model and model != "auto":
        cmd.extend(["--model", model])
    for attachment in attachments or []:
        cmd.extend(["--attachment", str(attachment)])
    if allow_file_read:
        # Scoped, read-only permissions: the CLI's agent reads a temp file we
        # wrote ourselves rather than the prompt carrying the full transcript
        # text as a command-line argument (which risks Windows CreateProcess
        # command-line length limits and observed related spawn failures on
        # large transcripts). No shell/write access is granted.
        cmd.extend(["--allow-tool", "read", "--deny-tool", "write", "--deny-tool", "shell"])

    # Repeated subprocess spawns of the Copilot CLI within one long-running
    # Python process can transiently fail (observed as FileNotFoundError)
    # after many prior invocations, even though the executable is confirmed
    # present and `copilot --version` works fine from a fresh shell. This is
    # not a quick blip, so use a longer, exponential backoff before giving up.
    #
    # Output is captured via temp files rather than subprocess.PIPE: pipe
    # handles that aren't fully released across dozens of sequential spawns
    # are a known source of this kind of persistent-in-process failure on
    # Windows, whereas file handles are closed deterministically here.
    max_attempts = 6
    backoff_seconds = 8
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        with tempfile.TemporaryDirectory(prefix="copilot_cli_") as tmp_dir:
            stdout_path = Path(tmp_dir) / "stdout.txt"
            stderr_path = Path(tmp_dir) / "stderr.txt"
            try:
                with open(stdout_path, "w", encoding="utf-8") as stdout_f, \
                     open(stderr_path, "w", encoding="utf-8") as stderr_f:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=stdout_f,
                        stderr=stderr_f,
                        text=True,
                    )
                    started = time.monotonic()
                    next_heartbeat = started + HEARTBEAT_SECONDS
                    returncode = None
                    while True:
                        try:
                            returncode = proc.wait(timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            now = time.monotonic()
                            elapsed = now - started
                            if elapsed >= COPILOT_TIMEOUT_SECONDS:
                                proc.kill()
                                proc.wait(timeout=10)
                                raise TimeoutError(
                                    f"Copilot CLI call exceeded {COPILOT_TIMEOUT_SECONDS}s timeout"
                                )
                            if now >= next_heartbeat:
                                print(f"\n    [still working ... {int(elapsed)}s elapsed]", end=" ", flush=True)
                                next_heartbeat = now + HEARTBEAT_SECONDS
                    if returncode != 0:
                        raise subprocess.CalledProcessError(returncode, cmd)
            except FileNotFoundError as exc:
                last_exc = exc
                if attempt < max_attempts:
                    wait = backoff_seconds * (2 ** (attempt - 1))
                    print(f"\n    [retry {attempt}/{max_attempts - 1}] Copilot CLI spawn failed, waiting {wait}s ...", end=" ", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    "GitHub Copilot CLI is not available. Install it and ensure 'copilot --version' works in this shell."
                ) from exc
            except TimeoutError:
                # A single call that never returns should not stall the whole
                # batch. Don't retry (a hang is unlikely to resolve itself);
                # surface a clear, distinguishable error so callers can log
                # this file for follow-up and move on to the next one.
                raise
            except subprocess.CalledProcessError as exc:
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                stdout = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
                detail = stderr or stdout or str(exc)
                raise RuntimeError(
                    f"Copilot CLI request failed: {detail}"
                ) from exc
            else:
                response = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
                if not response:
                    raise RuntimeError("Copilot CLI returned an empty response.")
                return response

    # Unreachable, but keeps type-checkers happy.
    raise RuntimeError("Copilot CLI request failed after retries.") from last_exc


def preflight_check() -> str | None:
    """Verify the Copilot CLI is installed, authenticated, and responsive
    before committing to a real batch run (which can take hours). Returns
    None on success, or a plain-English error message describing exactly
    what's wrong and how to fix it.

    This exists because a systemic failure (wrong GitHub account signed in,
    Copilot CLI not installed, no network) previously showed no clear signal
    to a non-technical user watching the terminal — the batch would either
    hang until each of 75 files individually timed out, or (worse) `run.py`
    would report "Run complete" with a failure count of 0 because nothing
    ever got the chance to log a per-file failure.
    """
    print("[preflight] checking Copilot CLI is installed and authenticated ...", end=" ", flush=True)
    try:
        reply = _run_copilot(
            "Reply with exactly the word: OK",
            model="auto",
        )
    except TimeoutError:
        print("FAILED")
        return (
            "The GitHub Copilot CLI did not respond within the timeout.\n"
            "This usually means it's stuck or unreachable (e.g. network issue).\n"
            "Try running 'copilot --version' in a terminal to confirm it works, then try again."
        )
    except RuntimeError as e:
        print("FAILED")
        return (
            f"The GitHub Copilot CLI check failed:\n  {e}\n\n"
            "Common causes:\n"
            "  - Not signed in, or signed in with the wrong GitHub account\n"
            "    (run 'gh auth status' to check, 'gh auth switch' to change accounts)\n"
            "  - The Copilot CLI isn't installed\n"
            "    (run 'copilot --version' to confirm it's on PATH)\n"
            "  - No network connection\n"
        )
    except Exception as e:  # noqa: BLE001 - deliberately broad: this is a safety gate
        print("FAILED")
        return f"Unexpected error while checking the Copilot CLI: {e}"

    if "OK" not in reply.upper():
        print("WARNING")
        # Not a hard failure — the CLI responded, just not with what we
        # expected. Could be a model quirk. Let the run proceed but flag it.
        print(
            f"[preflight] Copilot CLI responded, but not as expected (got: {reply[:80]!r}). "
            "Continuing anyway — if the real run also looks wrong, check 'gh auth status'."
        )
        return None

    print("OK")
    return None


def call_model(
    system: str,
    user: str,
    model: str,
    max_tokens: int = 4096,
    attachments: list[Path] | None = None,
) -> str:
    del max_tokens  # Copilot CLI does not currently expose a max-tokens flag.
    if attachments:
        raise RuntimeError("Text attachments are not supported by Copilot CLI in this workflow.")

    return _run_copilot(prompt=user, model=model)


def _chunk_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in text.split("\n\n"):
        para_len = len(para) + 2
        if para_len > max_chars:
            # Hard-wrap very large paragraphs.
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            for i in range(0, len(para), max_chars):
                chunks.append(para[i:i + max_chars])
            continue

        if current_len + para_len > max_chars and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def call_model_with_source(
    system: str,
    instruction: str,
    source_text: str,
    model: str,
) -> str:
    max_system_chars = 2400
    system_compact = system
    if len(system_compact) > max_system_chars:
        system_compact = (
            system_compact[:max_system_chars]
            + "\n\n[truncated for CLI prompt length safety]"
        )

    # Rather than passing source_text as part of the -p command-line
    # argument, write it to a temp file and have the Copilot CLI agent read
    # it itself (read-only tool permission). This avoids Windows
    # CreateProcess command-line length limits entirely and the related
    # intermittent spawn failures observed with large argv-embedded prompts.
    # Only fall back to splitting across multiple files/calls for sources
    # too large for a single agent turn to read comfortably.
    single_file_threshold = 500_000

    def _run_with_temp_file(text: str, prompt_builder) -> str:
        with tempfile.TemporaryDirectory(prefix="copilot_source_") as tmp_dir:
            src_path = Path(tmp_dir) / "source.txt"
            src_path.write_text(text, encoding="utf-8")
            return _run_copilot(
                prompt=prompt_builder(src_path),
                model=model,
                allow_file_read=True,
            )

    if len(source_text) <= single_file_threshold:
        def build_single_prompt(src_path: Path) -> str:
            return (
                "You are a transcript analysis assistant. Follow all guidance below exactly "
                "and produce only the requested output.\n\n"
                f"SYSTEM GUIDANCE:\n{system_compact}\n\n"
                f"TASK:\n{instruction}\n\n"
                f"SOURCE: Read the full source text from this file before analyzing it: {src_path}"
            )

        return _run_with_temp_file(source_text, build_single_prompt)

    chunks = _chunk_text(source_text, single_file_threshold)
    partials: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        def build_chunk_prompt(src_path: Path, i=i, total=len(chunks)) -> str:
            return (
                "You are a transcript analysis assistant. Follow all guidance below exactly. "
                "Analyze only this chunk and return structured notes that are faithful to the text.\n\n"
                f"SYSTEM GUIDANCE:\n{system_compact}\n\n"
                f"TASK:\n{instruction}\n\n"
                f"CHUNK {i}/{total}: Read the chunk text from this file before analyzing it: {src_path}"
            )

        partials.append(_run_with_temp_file(chunk, build_chunk_prompt))

    synthesis_prompt = (
        "You are a transcript analysis assistant. Synthesize the chunk analyses into one final answer. "
        "Preserve accuracy; avoid adding facts not in the chunks.\n\n"
        f"SYSTEM GUIDANCE:\n{system_compact}\n\n"
        f"TASK:\n{instruction}\n\n"
        "CHUNK ANALYSES:\n"
        + "\n\n---\n\n".join(
            f"Chunk {i + 1}:\n{part}" for i, part in enumerate(partials)
        )
    )
    return _run_copilot(prompt=synthesis_prompt, model=model)


def logs_dir() -> Path:
    return OUTPUT_PATH / "logs"


def run_log_path(timestamp: str) -> Path:
    return logs_dir() / f"run_{timestamp}.log"


def error_log_path(timestamp: str) -> Path:
    return logs_dir() / f"errors_{timestamp}.log"


class TeeStream:
    def __init__(self, *streams: TextIO):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def setup_run_logging() -> tuple[Path, Path]:
    logs_dir().mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_path = run_log_path(timestamp)
    err_path = error_log_path(timestamp)

    run_handle = open(run_path, "a", encoding="utf-8")
    err_handle = open(err_path, "a", encoding="utf-8")

    sys.stdout = TeeStream(sys.__stdout__, run_handle)
    sys.stderr = TeeStream(sys.__stderr__, run_handle, err_handle)

    print(f"[log] full run log -> {run_path}")
    print(f"[log] error log -> {err_path}")
    return run_path, err_path


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------

def _load_file(name: str) -> str:
    path = CONTEXT_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


_context_warning_shown = False


def build_system_prompt(transcript_prompt: str) -> str:
    brief = _load_file("program_brief.txt")
    rolodex = _load_file("rolodex.txt")

    # Missing context files degrade quality silently (no exception, no
    # visible symptom besides weaker output) — warn loudly once per run so
    # a non-technical user notices before assuming a full backfill is done.
    global _context_warning_shown
    if not _context_warning_shown:
        missing = []
        if not brief:
            missing.append("program_brief.txt")
        if not rolodex:
            missing.append("rolodex.txt")
        if missing:
            print(
                f"[warn] client_context/ is missing: {', '.join(missing)} — "
                "analysis will run, but quality will be noticeably worse without "
                "this context (see client_context/README.txt for setup)."
            )
        _context_warning_shown = True

    parts = []
    if brief:
        parts.append(brief)
    if rolodex:
        parts.append("## People Reference\n" + rolodex)
    parts.append(transcript_prompt)
    return "\n\n---\n\n".join(parts)


def load_solution_prompt() -> str:
    text = _load_file("solution_prompt.txt")
    if not text:
        # Fallback if prompt file not yet populated
        return (
            "You are analyzing a technical design or discovery call transcript. "
            "Extract and structure:\n"
            "1. **Requirements Discussed** — explicit or implied functional/non-functional requirements\n"
            "2. **Decisions Made** — what was agreed or confirmed\n"
            "3. **Open Items / Deferred** — questions or decisions left unresolved\n"
            "4. **Constraints Surfaced** — technical, organizational, or timeline constraints\n"
            "5. **Key Stakeholders Present** — named individuals and their stated positions\n"
            "6. **Private Read** — internal observations about dynamics, risks, or subtext (candid)\n\n"
            "## Private read — internal only"
        )
    return text


# ---------------------------------------------------------------------------
# VTT → clean text
# ---------------------------------------------------------------------------

# Matches lines like: 00:00:00.000 --> 00:00:05.000 or WEBVTT header / NOTE / cue numbers
_VTT_SKIP = re.compile(
    r"^(WEBVTT|NOTE|STYLE|REGION)"
    r"|^\d+$"
    r"|^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->"
    r"|^$",
    re.IGNORECASE,
)

# Speaker pattern: "Speaker Name: text" — Teams/Zoom usually emits this
_SPEAKER_LINE = re.compile(r"^([^:]{2,60}):\s+(.+)$")


def vtt_to_text(vtt_path: Path) -> str:
    lines = _safe_read_text(vtt_path, encoding="utf-8", errors="replace").splitlines()
    segments: list[tuple[str, str]] = []  # (speaker, text)

    current_speaker = ""
    current_text: list[str] = []

    def flush():
        if current_text:
            joined = " ".join(current_text).strip()
            if joined:
                segments.append((current_speaker, joined))

    for line in lines:
        if _VTT_SKIP.match(line):
            continue

        # Strip inline VTT tags like <00:00:01.000><c>text</c>
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if not clean:
            continue

        m = _SPEAKER_LINE.match(clean)
        if m:
            speaker, text = m.group(1).strip(), m.group(2).strip()
            if speaker != current_speaker:
                flush()
                current_speaker = speaker
                current_text = [text]
            else:
                current_text.append(text)
        else:
            # Continuation line without speaker label
            current_text.append(clean)

    flush()

    # Deduplicate consecutive identical segments (VTT sometimes repeats)
    deduped: list[tuple[str, str]] = []
    for seg in segments:
        if not deduped or seg != deduped[-1]:
            deduped.append(seg)

    return "\n".join(
        f"{spk}: {txt}" if spk else txt for spk, txt in deduped
    )


def transcript_to_text(path: Path) -> str:
    if path.suffix.lower() == ".vtt":
        return vtt_to_text(path)
    return _safe_read_text(path, encoding="utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_bus(root: Path) -> dict[str, list[Path]]:
    """Return {bu_name: [transcript_paths]} grouped by immediate subfolder."""
    bus: dict[str, list[Path]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".vtt", ".txt"):
            continue
        # BU = immediate child folder of root; files directly in root go to "ROOT"
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        bu = rel.parts[0] if len(rel.parts) > 1 else "ROOT"
        bus.setdefault(bu, []).append(path)
    return bus


def analyzed_path(vtt: Path, bu: str) -> Path:
    stem = vtt.stem
    return OUTPUT_PATH / bu / f"{stem} [ANALYZED].txt"


def analyzed_meta_path(vtt: Path, bu: str) -> Path:
    stem = vtt.stem
    return OUTPUT_PATH / bu / f"{stem} [ANALYZED].meta.json"


def summary_path(bu: str) -> Path:
    return OUTPUT_PATH / bu / f"[BU SUMMARY] {bu}.md"


def qc_report_path() -> Path:
    return OUTPUT_PATH / "[QC REPORT] transcript_quality.md"


def needs_followup_path() -> Path:
    return OUTPUT_PATH / "NEEDS_FOLLOWUP.txt"


def log_needs_followup(bu: str, vtt_name: str, reason: str) -> None:
    """Append a one-line record of a failed/timed-out transcript so problem
    files are easy to spot and revisit later, without blocking the batch."""
    timestamp = datetime.now(UTC).isoformat()
    try:
        _safe_append_text(needs_followup_path(), f"{timestamp} | {bu} | {vtt_name} | {reason}\n")
    except Exception as e:
        # Never let follow-up bookkeeping itself crash the batch.
        print(f"  [warn] could not write NEEDS_FOLLOWUP entry: {e}", file=sys.stderr)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_signature(transcript: Path) -> dict[str, object]:
    raw = transcript.read_bytes()
    stat = transcript.stat()
    return {
        "source_path": str(transcript.resolve()),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_sha256": _sha256_bytes(raw),
    }


def _system_signature(system: str) -> str:
    return _sha256_bytes(system.encode("utf-8"))


def _read_meta(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        return json.loads(_safe_read_text(path, encoding="utf-8"))
    except Exception:
        return None


def _write_meta(path: Path, payload: dict[str, object]) -> None:
    _safe_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _is_analysis_fresh(meta: dict[str, object], source_sig: dict[str, object], system_sig: str) -> bool:
    required = {
        "status": "ok",
        "source_sha256": source_sig["source_sha256"],
        "source_size": source_sig["source_size"],
        "source_mtime_ns": source_sig["source_mtime_ns"],
        "model_transcript": MODEL_TRANSCRIPT,
        "system_signature": system_sig,
    }
    return all(meta.get(k) == v for k, v in required.items())


def _is_qc_block_fresh(
    meta: dict[str, object],
    source_sig: dict[str, object],
    system_sig: str,
    qc_threshold: int,
) -> bool:
    required = {
        "status": "qc_blocked",
        "source_sha256": source_sig["source_sha256"],
        "source_size": source_sig["source_size"],
        "source_mtime_ns": source_sig["source_mtime_ns"],
        "model_transcript": MODEL_TRANSCRIPT,
        "system_signature": system_sig,
        "qc_threshold": qc_threshold,
    }
    return all(meta.get(k) == v for k, v in required.items())


def _quality_metrics(text: str) -> dict[str, float | int]:
    words = re.findall(r"\b\w+\b", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    line_count = len(lines)
    unique_line_count = len(set(lines)) if line_count else 0
    duplicate_ratio = 0.0 if line_count == 0 else 1.0 - (unique_line_count / line_count)
    inaudible_hits = len(re.findall(r"\b(inaudible|unintelligible|unclear)\b|\[[^\]]*(inaudible|unintelligible|unclear)[^\]]*\]", text, flags=re.IGNORECASE))

    return {
        "chars": len(text),
        "words": len(words),
        "lines": line_count,
        "duplicate_ratio": duplicate_ratio,
        "inaudible_hits": inaudible_hits,
    }


def _quality_assessment(metrics: dict[str, float | int]) -> tuple[int, str, list[str]]:
    score = 100
    flags: list[str] = []

    chars = int(metrics["chars"])
    words = int(metrics["words"])
    lines = int(metrics["lines"])
    duplicate_ratio = float(metrics["duplicate_ratio"])
    inaudible_hits = int(metrics["inaudible_hits"])

    if chars < 400:
        score -= 40
        flags.append("very short parsed text")
    elif chars < 1000:
        score -= 20
        flags.append("short parsed text")

    if words < 80:
        score -= 25
        flags.append("low word count")

    if lines < 10:
        score -= 20
        flags.append("few speaker/text lines")

    if duplicate_ratio > 0.5:
        score -= 35
        flags.append("high repeated content")
    elif duplicate_ratio > 0.3:
        score -= 20
        flags.append("moderate repeated content")

    if inaudible_hits >= 10:
        score -= 25
        flags.append("many inaudible/unclear markers")
    elif inaudible_hits >= 3:
        score -= 10
        flags.append("some inaudible/unclear markers")

    score = max(0, min(100, score))
    if score >= 75:
        label = "good"
    elif score >= 50:
        label = "watch"
    else:
        label = "poor"

    return score, label, flags


def generate_qc_report(bus: dict[str, list[Path]]) -> None:
    out = qc_report_path()
    out.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    for bu, transcripts in sorted(bus.items()):
        for transcript in transcripts:
            try:
                text = transcript_to_text(transcript)
                metrics = _quality_metrics(text)
                score, label, flags = _quality_assessment(metrics)
                rows.append({
                    "path": str(transcript),
                    "bucket": bu,
                    "score": score,
                    "label": label,
                    "chars": metrics["chars"],
                    "words": metrics["words"],
                    "lines": metrics["lines"],
                    "duplicate_ratio": metrics["duplicate_ratio"],
                    "inaudible_hits": metrics["inaudible_hits"],
                    "flags": flags,
                })
            except Exception as e:
                rows.append({
                    "path": str(transcript),
                    "bucket": bu,
                    "score": 0,
                    "label": "error",
                    "chars": 0,
                    "words": 0,
                    "lines": 0,
                    "duplicate_ratio": 1.0,
                    "inaudible_hits": 0,
                    "flags": [f"parse error: {e}"],
                })

    rows.sort(key=lambda r: (int(r["score"]), str(r["path"])))
    total = len(rows)
    poor = sum(1 for r in rows if r["label"] in ("poor", "error"))
    watch = sum(1 for r in rows if r["label"] == "watch")
    good = sum(1 for r in rows if r["label"] == "good")

    lines: list[str] = []
    lines.append("# Transcript Quality Report\n")
    lines.append("Heuristic QC to detect likely transcription quality problems before model analysis.\n")
    lines.append(f"Generated: {datetime.now(UTC).isoformat()}  ")
    lines.append(f"Total transcripts scanned: **{total}**  ")
    lines.append(f"Good: **{good}** | Watch: **{watch}** | Poor/Error: **{poor}**\n")
    lines.append("## Highest-Risk Transcripts\n")
    risky = [r for r in rows if r["label"] in ("poor", "error")][:25]
    if risky:
        for r in risky:
            flags = "; ".join(r["flags"]) if r["flags"] else "none"
            lines.append(f"- **[{r['score']}/100 {r['label']}]** {r['path']} ({r['bucket']})")
            lines.append(f"  - flags: {flags}")
    else:
        lines.append("- No high-risk transcripts detected by heuristics.")

    lines.append("\n## Full QC Table\n")
    lines.append("| Score | Label | Bucket | Chars | Words | Lines | Dup Ratio | Inaudible | File |")
    lines.append("|------:|-------|--------|------:|------:|------:|----------:|----------:|------|")
    for r in rows:
        lines.append(
            "| "
            f"{r['score']} | {r['label']} | {r['bucket']} | {r['chars']} | {r['words']} | {r['lines']} | "
            f"{float(r['duplicate_ratio']):.2f} | {r['inaudible_hits']} | {r['path']} |"
        )

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [qc report] written -> {out}")


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def _try_record_failure(
    meta_out: Path,
    source_sig: dict[str, object],
    system_sig: str,
    qc_threshold: int,
    status: str,
    error: str,
) -> None:
    """Best-effort meta write for a failed/timed-out transcript. Never
    raises: a secondary I/O failure while handling the first one should
    not crash the whole batch run."""
    try:
        _write_meta(meta_out, {
            **source_sig,
            "status": status,
            "error": error,
            "qc_threshold": qc_threshold,
            "model_transcript": MODEL_TRANSCRIPT,
            "system_signature": system_sig,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        })
    except Exception as e:
        print(f"  [warn] could not write failure meta for {meta_out.name}: {e}", file=sys.stderr)


def analyze_transcript(vtt: Path, bu: str, system: str, qc_threshold: int = 0) -> str | None:
    tag = _progress_tag()
    out = analyzed_path(vtt, bu)
    meta_out = analyzed_meta_path(vtt, bu)
    source_sig = _source_signature(vtt)
    system_sig = _system_signature(system)
    meta = _read_meta(meta_out)
    if out.exists():
        if meta and _is_analysis_fresh(meta, source_sig, system_sig):
            print(f"  {tag}[skip] {vtt.name} — already analyzed (fresh cache)")
            return _safe_read_text(out, encoding="utf-8")
        print(f"  {tag}[reprocess] {vtt.name} — source/prompt/model changed or legacy cache")
    elif meta and qc_threshold > 0 and _is_qc_block_fresh(meta, source_sig, system_sig, qc_threshold):
        print(f"  {tag}[skip] {vtt.name} — blocked by QC threshold ({qc_threshold})")
        return None

    print(f"  {tag}[analyze] {vtt.name} ...", end=" ", flush=True)
    try:
        text = transcript_to_text(vtt)
        metrics = _quality_metrics(text)
        qc_score, qc_label, qc_flags = _quality_assessment(metrics)

        if qc_threshold > 0 and qc_score < qc_threshold:
            print(f"SKIP (qc {qc_score} < {qc_threshold})")
            _write_meta(meta_out, {
                **source_sig,
                "status": "qc_blocked",
                "reason": "transcript quality below threshold",
                "qc_threshold": qc_threshold,
                "qc_score": qc_score,
                "qc_label": qc_label,
                "qc_flags": qc_flags,
                "text_chars": len(text),
                "model_transcript": MODEL_TRANSCRIPT,
                "system_signature": system_sig,
                "updated_at_utc": datetime.now(UTC).isoformat(),
            })
            return None

        if len(text) < 100:
            print("SKIP (too short after VTT parse)")
            _write_meta(meta_out, {
                **source_sig,
                "status": "too_short",
                "reason": "parsed transcript shorter than 100 characters",
                "qc_threshold": qc_threshold,
                "qc_score": qc_score,
                "qc_label": qc_label,
                "qc_flags": qc_flags,
                "text_chars": len(text),
                "model_transcript": MODEL_TRANSCRIPT,
                "system_signature": system_sig,
                "updated_at_utc": datetime.now(UTC).isoformat(),
            })
            return None

        result = call_model_with_source(
            system=system,
            instruction=(
                "Analyze this transcript and return the required structured extraction. "
                f"Transcript filename: {vtt.name}"
            ),
            source_text=text,
            model=MODEL_TRANSCRIPT,
        )
        _safe_write_text(out, result, encoding="utf-8")
        _write_meta(meta_out, {
            **source_sig,
            "status": "ok",
            "qc_threshold": qc_threshold,
            "qc_score": qc_score,
            "qc_label": qc_label,
            "qc_flags": qc_flags,
            "text_chars": len(text),
            "result_chars": len(result),
            "model_transcript": MODEL_TRANSCRIPT,
            "system_signature": system_sig,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        })
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
        return result
    except TimeoutError as e:
        print(f"TIMEOUT: {e}", file=sys.stderr)
        _try_record_failure(meta_out, source_sig, system_sig, qc_threshold, "timeout", str(e))
        log_needs_followup(bu, vtt.name, f"TIMEOUT: {e}")
        return None
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        _try_record_failure(meta_out, source_sig, system_sig, qc_threshold, "error", str(e))
        log_needs_followup(bu, vtt.name, f"ERROR: {e}")
        return None


def summarize_bu(bu: str, analyses: list[str], system: str) -> None:
    out = summary_path(bu)
    print(f"  [summarize] {bu} ({len(analyses)} analyses) ...", end=" ", flush=True)

    bundle = "\n\n---\n\n".join(
        f"### Analysis {i + 1}\n{a}" for i, a in enumerate(analyses)
    )

    prompt = (
        f"The attached file contains individual analyses of discovery/design calls from the {bu} business unit.\n\n"
        "Synthesize across all of them:\n"
        "1. Confirmed requirements\n"
        "2. Open decisions\n"
        "3. Stated constraints\n"
        "4. Key themes\n"
        "5. Named stakeholders and positions\n"
        "6. Top 3 unknowns that block design progress"
    )

    try:
        result = call_model_with_source(
            system=system,
            instruction=prompt,
            source_text=bundle,
            model=MODEL_SUMMARY,
        )
        _safe_write_text(out, result, encoding="utf-8")
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        log_needs_followup(bu, "[BU SUMMARY]", f"ERROR: {e}")


def process_bu(
    bu: str,
    vtts: list[Path],
    system: str,
    summary_only: bool = False,
    transcript_only: bool = False,
    qc_threshold: int = 0,
) -> None:
    print(f"  {len(vtts)} transcript(s)")
    analyses: list[str] = []

    if summary_only:
        # Load existing analyses
        for vtt in vtts:
            out = analyzed_path(vtt, bu)
            if out.exists():
                analyses.append(_safe_read_text(out, encoding="utf-8"))
            else:
                print(f"  [warn] no analysis found for {vtt.name} — skipping from summary")
    else:
        for vtt in vtts:
            # Defense-in-depth: analyze_transcript already catches its own
            # errors/timeouts, but a genuinely unexpected exception here
            # should never be allowed to kill the whole batch run. Log it
            # and keep processing the remaining transcripts/BUs.
            try:
                result = analyze_transcript(vtt, bu, system, qc_threshold=qc_threshold)
            except Exception as e:
                print(f"  [ERROR] unexpected failure analyzing {vtt.name}: {e}", file=sys.stderr)
                log_needs_followup(bu, vtt.name, f"UNEXPECTED ERROR: {e}")
                result = None
            if result:
                analyses.append(result)

    if transcript_only:
        print(f"  [skip summary] transcript-only mode for {bu}")
        return

    if analyses:
        summarize_bu(bu, analyses, system)
    else:
        print(f"  [warn] no analyses available for {bu}, skipping summary")


# ===========================================================================
# UC Pipeline — output paths
# ===========================================================================

def uc_analyzed_path(transcript: Path, feature_key: str) -> Path:
    return FEATURES_OUT / feature_key / f"{transcript.stem} [ANALYZED].txt"


def uc_analyzed_meta_path(transcript: Path, feature_key: str) -> Path:
    return FEATURES_OUT / feature_key / f"{transcript.stem} [ANALYZED].meta.json"


def uc_feature_summary_path(feature_key: str, display_name: str) -> Path:
    safe = re.sub(r'[<>:"/\\|?*]', "-", display_name)
    return FEATURES_OUT / feature_key / f"[FEATURE SUMMARY] {safe}.md"


def uc_summary_path(uc: str) -> Path:
    return OUTPUT_PATH / f"[UC SUMMARY] {uc}.md"


def gap_report_path() -> Path:
    return OUTPUT_PATH / "[GAP REPORT] coverage.md"


def _feat_key(feat: Feature) -> str:
    return feat.prefix or re.sub(r"[^a-z0-9]+", "-", feat.arm_name.lower()).strip("-")


# ===========================================================================
# UC Pipeline — prompts
# ===========================================================================

def _build_uc_transcript_prompt(feat: Feature, base_context: str, solution_prompt: str) -> str:
    uc_scope = ", ".join(uc for uc in ("UC0", "UC1", "UC2") if feat.in_scope(uc)) or "TBD"
    feature_block = (
        f"## Analysis Focus: {feat.display_name}\n\n"
        f"You are analyzing a transcript specifically for the **{feat.display_name}** capability.\n\n"
        f"**Feature Definition:**\n{feat.definition or '(no definition available)'}\n\n"
        f"**In scope for:** {uc_scope}\n\n"
        "Extract only information relevant to this feature. Structure your output:\n\n"
        "1. **Requirements Confirmed** — explicit functional/non-functional requirements discussed\n"
        "2. **Decisions Made** — design or scoping decisions confirmed\n"
        "3. **Open Items** — unresolved questions or TBD decisions\n"
        "4. **Constraints** — technical, timeline, or org constraints\n"
        "5. **Coverage Gaps** — things that should have been discussed but weren't\n"
        "6. **Key Participants** — who spoke and their stated position\n"
        "7. **Private Read** — candid assessment: confidence level, political subtext, risks\n\n"
        f"If this transcript has no meaningful discussion of **{feat.display_name}**, "
        "say so in one sentence and stop."
    )
    parts = []
    if base_context:
        parts.append(base_context)
    if solution_prompt:
        parts.append("## Global Transcript Extraction Guidance\n" + solution_prompt)
    parts.append(feature_block)
    return "\n\n---\n\n".join(parts)


def _build_feature_summary_prompt(feat: Feature, analyses: list[str]) -> str:
    uc_scope = ", ".join(uc for uc in ("UC0", "UC1", "UC2") if feat.in_scope(uc)) or "TBD"
    bundle = "\n\n---\n\n".join(f"### Transcript Analysis {i+1}\n{a}" for i, a in enumerate(analyses))
    return (
        f"The following are {len(analyses)} transcript analyses for the "
        f"**{feat.display_name}** capability (in scope: {uc_scope}).\n\n"
        f"Feature definition:\n{feat.definition or '(no definition available)'}\n\n"
        "Synthesize across all analyses:\n\n"
        "1. **Confirmed Scope** — what's definitively in or out of scope\n"
        "2. **Key Decisions** — design choices agreed\n"
        "3. **Open Items** — questions still unresolved across all calls\n"
        "4. **Coverage Confidence** — how well do the transcripts cover this feature? What's still dark?\n"
        "5. **Top 3 Unknowns** — highest-priority gaps before design can proceed\n"
        "6. **Owner / Stakeholders** — who owns this and who has strong opinions\n\n"
        "---\n\n" + bundle
    )


def _build_uc_summary_prompt(uc: str, feature_summaries: list[tuple[str, str]]) -> str:
    feature_list = "\n".join(f"- {name}" for name, _ in feature_summaries)
    bundle = "\n\n---\n\n".join(f"### {name}\n{summary}" for name, summary in feature_summaries)
    return (
        f"The following are feature summaries for **{UC_NAMES[uc]}**.\n\n"
        f"Features covered:\n{feature_list}\n\n"
        "Synthesize across all features for this use case:\n\n"
        "1. **UC Scope Summary** — what this UC is trying to accomplish end-to-end\n"
        "2. **Confirmed Design Decisions** — locked choices across features\n"
        "3. **Cross-Feature Dependencies** — where features interact or sequence matters\n"
        "4. **Top Open Items** — highest-priority unresolved items across the UC\n"
        "5. **Coverage Gaps** — in-scope features with weak or no transcript coverage\n"
        "6. **Readiness Assessment** — overall confidence we understand this UC well enough to start HLD\n\n"
        "---\n\n" + bundle
    )


# ===========================================================================
# UC Pipeline — processing functions
# ===========================================================================

def _uc_is_analysis_fresh(
    meta: dict,
    source_sig: dict,
    system_sig: str,
) -> bool:
    required = {
        "status": "ok",
        "source_sha256": source_sig["source_sha256"],
        "source_size": source_sig["source_size"],
        "source_mtime_ns": source_sig["source_mtime_ns"],
        "model_transcript": MODEL_TRANSCRIPT,
        "system_signature": system_sig,
    }
    return all(meta.get(k) == v for k, v in required.items())


def _uc_is_qc_block_fresh(meta: dict, source_sig: dict, system_sig: str, qc_threshold: int) -> bool:
    required = {
        "status": "qc_blocked",
        "source_sha256": source_sig["source_sha256"],
        "source_size": source_sig["source_size"],
        "source_mtime_ns": source_sig["source_mtime_ns"],
        "model_transcript": MODEL_TRANSCRIPT,
        "system_signature": system_sig,
        "qc_threshold": qc_threshold,
    }
    return all(meta.get(k) == v for k, v in required.items())


def uc_analyze_transcript(
    transcript: Path,
    feat: Feature,
    base_context: str,
    solution_prompt: str,
    qc_threshold: int = 0,
    counter: str = "",
) -> "str | None":
    key = _feat_key(feat)
    out = uc_analyzed_path(transcript, key)
    meta_out = uc_analyzed_meta_path(transcript, key)
    source_sig = _source_signature(transcript)
    system = _build_uc_transcript_prompt(feat, base_context, solution_prompt)
    system_sig = _system_signature(system)
    meta = _read_meta(meta_out)

    prefix = f"    {counter} " if counter else "    "

    if out.exists():
        if meta and _uc_is_analysis_fresh(meta, source_sig, system_sig):
            print(f"{prefix}[skip] {transcript.name} — already analyzed")
            return out.read_text(encoding="utf-8")
        print(f"{prefix}[reprocess] {transcript.name} — source/prompt/model changed")
    elif meta and qc_threshold > 0 and _uc_is_qc_block_fresh(meta, source_sig, system_sig, qc_threshold):
        print(f"{prefix}[skip] {transcript.name} — blocked by QC threshold ({qc_threshold})")
        return None

    print(f"{prefix}[analyze] {transcript.name} ...", end=" ", flush=True)
    try:
        text = transcript_to_text(transcript)
        metrics = _quality_metrics(text)
        qc_score, qc_label, qc_flags = _quality_assessment(metrics)

        if qc_threshold > 0 and qc_score < qc_threshold:
            print(f"SKIP (qc {qc_score} < {qc_threshold})")
            _write_meta(meta_out, {
                **source_sig,
                "status": "qc_blocked",
                "reason": "transcript quality below threshold",
                "qc_threshold": qc_threshold,
                "qc_score": qc_score,
                "qc_label": qc_label,
                "qc_flags": qc_flags,
                "text_chars": len(text),
                "model_transcript": MODEL_TRANSCRIPT,
                "system_signature": system_sig,
                "updated_at_utc": datetime.now(UTC).isoformat(),
            })
            return None

        if len(text) < 100:
            print("SKIP (too short)")
            _write_meta(meta_out, {
                **source_sig,
                "status": "too_short",
                "reason": "parsed transcript shorter than 100 characters",
                "qc_threshold": qc_threshold,
                "qc_score": qc_score,
                "qc_label": qc_label,
                "qc_flags": qc_flags,
                "text_chars": len(text),
                "model_transcript": MODEL_TRANSCRIPT,
                "system_signature": system_sig,
                "updated_at_utc": datetime.now(UTC).isoformat(),
            })
            return None

        result = call_model_with_source(
            system=system,
            instruction=(
                f"Analyze this transcript for the {feat.display_name} capability. "
                f"Transcript filename: {transcript.name}"
            ),
            source_text=text,
            model=MODEL_TRANSCRIPT,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result, encoding="utf-8")
        _write_meta(meta_out, {
            **source_sig,
            "status": "ok",
            "qc_threshold": qc_threshold,
            "qc_score": qc_score,
            "qc_label": qc_label,
            "qc_flags": qc_flags,
            "text_chars": len(text),
            "result_chars": len(result),
            "model_transcript": MODEL_TRANSCRIPT,
            "system_signature": system_sig,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        })
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
        return result
    except TimeoutError as e:
        print(f"TIMEOUT: {e}", file=sys.stderr)
        _write_meta(meta_out, {
            **source_sig,
            "status": "timeout",
            "error": str(e),
            "qc_threshold": qc_threshold,
            "model_transcript": MODEL_TRANSCRIPT,
            "system_signature": system_sig,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        })
        log_needs_followup(key, transcript.name, f"TIMEOUT: {e}")
        return None
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        _write_meta(meta_out, {
            **source_sig,
            "status": "error",
            "error": str(e),
            "qc_threshold": qc_threshold,
            "model_transcript": MODEL_TRANSCRIPT,
            "system_signature": system_sig,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        })
        log_needs_followup(key, transcript.name, f"ERROR: {e}")
        return None


def uc_summarize_feature(
    feat: Feature,
    analyses: list[str],
    base_context: str,
    feat_num: int,
    feat_total: int,
) -> "str | None":
    key = _feat_key(feat)
    out = uc_feature_summary_path(key, feat.display_name)
    counter = f"[{feat_num}/{feat_total}]"

    if out.exists():
        print(f"  {counter} [skip summary] {feat.display_name}")
        return out.read_text(encoding="utf-8")

    print(f"  {counter} [feature summary] {feat.display_name} ({len(analyses)} analyses) ...", end=" ", flush=True)
    try:
        result = call_model_with_source(
            system=base_context or "You are an expert Salesforce Revenue Cloud architect.",
            instruction=_build_feature_summary_prompt(feat, analyses),
            source_text="\n\n---\n\n".join(analyses),
            model=MODEL_SUMMARY,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result, encoding="utf-8")
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
        return result
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None


def uc_summarize_uc(uc: str, feature_summaries: list[tuple[str, str]], base_context: str) -> None:
    out = uc_summary_path(uc)
    if out.exists():
        print(f"  [skip] {uc} summary already exists — delete to regenerate")
        return

    print(f"  [UC summary] {uc} ({len(feature_summaries)} features) ...", end=" ", flush=True)
    if not feature_summaries:
        print("SKIP (no feature summaries)")
        return
    try:
        result = call_model_with_source(
            system=base_context or "You are an expert Salesforce Revenue Cloud architect.",
            instruction=_build_uc_summary_prompt(uc, feature_summaries),
            source_text="\n\n---\n\n".join(s for _, s in feature_summaries),
            model=MODEL_SUMMARY,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result, encoding="utf-8")
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)


# ===========================================================================
# UC Pipeline — gap report (no API calls)
# ===========================================================================

def generate_gap_report(registry: dict[str, Feature], unmatched_folders: list[Path]) -> None:
    out = gap_report_path()
    out.parent.mkdir(parents=True, exist_ok=True)

    features = list(registry.values())
    with_folder = [f for f in features if f.folder is not None]
    without_folder = [f for f in features if f.folder is None]

    def transcript_count(feat: Feature) -> int:
        if feat.folder is None:
            return 0
        return sum(1 for p in feat.folder.iterdir()
                   if p.is_file() and p.suffix.lower() in (".vtt", ".txt"))

    def analysis_count(feat: Feature) -> int:
        d = FEATURES_OUT / _feat_key(feat)
        if not d.exists():
            return 0
        return sum(1 for p in d.iterdir() if "[ANALYZED]" in p.name and p.suffix == ".txt")

    lines: list[str] = [
        "# Coverage Gap Report\n",
        f"Generated: {datetime.now(UTC).isoformat()}  ",
        f"Total features in backlog: **{len(features)}**  ",
        f"Features with transcript folders: **{len(with_folder)}**  ",
        f"Features without transcript folders: **{len(without_folder)}**\n",
        "## Coverage Matrix\n",
        "| Feature | ARM Capability | UC0 | UC1 | UC2 | Transcripts | Analyzed |",
        "|---------|---------------|-----|-----|-----|-------------|----------|",
    ]

    for feat in sorted(features, key=lambda f: (f.prefix or "z", f.arm_name)):
        tc = transcript_count(feat)
        ac = analysis_count(feat)
        status = f"{ac}/{tc}" if tc else "**NO COVERAGE**"
        lines.append(
            f"| {feat.prefix or '-'} "
            f"| {feat.arm_name} "
            f"| {feat.scope_label('UC0')} "
            f"| {feat.scope_label('UC1')} "
            f"| {feat.scope_label('UC2')} "
            f"| {tc} "
            f"| {status} |"
        )

    # Priority gaps: in-scope for at least one UC, no transcripts
    in_scope_no_coverage = [
        f for f in without_folder
        if any(f.in_scope(uc) for uc in ("UC0", "UC1", "UC2"))
    ]

    lines.append("\n## Priority Gaps (in-scope, no recordings)\n")
    if in_scope_no_coverage:
        for feat in sorted(in_scope_no_coverage, key=lambda f: (f.prefix or "z")):
            ucs = [uc for uc in ("UC0", "UC1", "UC2") if feat.in_scope(uc)]
            lines.append(f"- **{feat.display_name}** — in scope for {', '.join(ucs)}")
            if feat.definition:
                first_sentence = feat.definition.split(".")[0].strip()
                lines.append(f"  _{first_sentence}_")
    else:
        lines.append("_All in-scope features have at least one transcript folder._")

    if unmatched_folders:
        lines.append("\n## Unmatched Folders (not in backlog CSVs)\n")
        lines.append("These folders were found but could not be matched to a backlog feature:\n")
        for p in unmatched_folders:
            lines.append(f"- `{p.name}`")

    not_in_scope = [f for f in features if not any(f.in_scope(uc) for uc in ("UC0", "UC1", "UC2"))]
    if not_in_scope:
        lines.append("\n## Out-of-Scope Features (no UC coverage required)\n")
        for feat in sorted(not_in_scope, key=lambda f: f.prefix or "z"):
            lines.append(f"- {feat.prefix or '-'} {feat.arm_name}")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [gap report] written -> {out}")


# ===========================================================================
# UC Pipeline — main entry point
# ===========================================================================

def _find_transcripts(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in (".vtt", ".txt"))


def run_uc_pipeline(
    summary_only: bool = False,
    gap_only: bool = False,
    qc_only: bool = False,
    feature_filter: "str | None" = None,
    qc_threshold: int = 0,
) -> None:
    setup_run_logging()

    if not TRANSCRIPTS_PATH_RAW:
        sys.exit("TRANSCRIPTS_PATH is missing. Set it in .env")
    if not TRANSCRIPTS_PATH.exists():
        sys.exit(f"TRANSCRIPTS_PATH not found: {TRANSCRIPTS_PATH}")

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    FEATURES_OUT.mkdir(parents=True, exist_ok=True)

    missing_csvs = [p for p in required_csv_paths() if not p.exists()]
    if missing_csvs and not qc_only:
        sys.exit(
            "Required backlog CSV files are missing. Place them under client_data/.\n"
            "Missing:\n" + "\n".join(f"- {p}" for p in missing_csvs)
        )

    print("Loading feature registry from backlog CSVs...")
    registry = load_feature_registry() if not missing_csvs else {}
    if registry:
        print(f"  {len(registry)} features loaded")

    print("Matching transcript folders to features...")
    unmatched = match_folders_to_features(registry) if registry else []
    matched = sum(1 for f in registry.values() if f.folder is not None)
    if registry:
        print(f"  {matched} features matched to folders, {len(unmatched)} unmatched folders")
        if unmatched:
            print(f"  Unmatched folders: {[p.name for p in unmatched]}")

    # Count total transcripts and MP4s for the user
    all_vtts = list(TRANSCRIPTS_PATH.rglob("*.vtt")) + list(TRANSCRIPTS_PATH.rglob("*.txt"))
    all_mp4s = list(TRANSCRIPTS_PATH.rglob("*.mp4"))
    print(f"\n  Transcripts found  : {len(all_vtts)}")
    if all_mp4s:
        print(f"  Video files found  : {len(all_mp4s)}  (run transcribe_batch.py to convert)")

    if gap_only:
        print("\n=== Generating gap report ===")
        generate_gap_report(registry, unmatched)
        return

    if qc_only:
        bus_for_qc = {folder.name: _find_transcripts(folder)
                      for folder in TRANSCRIPTS_PATH.iterdir() if folder.is_dir()}
        generate_qc_report(bus_for_qc)
        return

    base_context = build_system_prompt("")  # loads brief + rolodex
    solution_prompt = load_solution_prompt()

    if feature_filter:
        feat = registry.get(feature_filter)
        if not feat:
            sys.exit(f"Feature '{feature_filter}' not found. Available: {sorted(registry)}")
        if feat.folder is None:
            sys.exit(f"Feature '{feature_filter}' has no matched transcript folder.")
        transcripts = _find_transcripts(feat.folder)
        analyses: list[str] = []
        if not summary_only:
            for i, t in enumerate(transcripts, 1):
                result = uc_analyze_transcript(
                    t, feat, base_context, solution_prompt, qc_threshold,
                    counter=f"[{i}/{len(transcripts)}]",
                )
                if result:
                    analyses.append(result)
        else:
            key = _feat_key(feat)
            for p in sorted((FEATURES_OUT / key).iterdir()):
                if "[ANALYZED]" in p.name and p.suffix == ".txt":
                    analyses.append(p.read_text(encoding="utf-8"))
        if analyses:
            uc_summarize_feature(feat, analyses, base_context, 1, 1)
        print("\n=== Generating gap report ===")
        generate_gap_report(registry, unmatched)
        return

    # Full pipeline: all features → UC rollups → gap report
    features_with_folders = [
        (key, feat) for key, feat in sorted(registry.items(), key=lambda kv: (kv[1].prefix or "z", kv[1].arm_name))
        if feat.folder is not None
    ]
    feat_total = len(features_with_folders)
    print(f"\n=== Analyzing transcripts across {feat_total} features ===")

    feature_summaries_by_uc: dict[str, list[tuple[str, str]]] = {uc: [] for uc in UC_NAMES}

    for feat_num, (key, feat) in enumerate(features_with_folders, 1):
        transcripts = _find_transcripts(feat.folder)
        feat_counter = f"[{feat_num}/{feat_total}]"
        print(f"\n  {feat_counter} Feature: {feat.display_name} — {len(transcripts)} transcript(s)")

        analyses = []
        if summary_only:
            for p in sorted((FEATURES_OUT / key).iterdir()):
                if "[ANALYZED]" in p.name and p.suffix == ".txt":
                    analyses.append(p.read_text(encoding="utf-8"))
            if not analyses:
                print(f"    [warn] no existing analyses found for {feat.display_name}")
        else:
            for i, t in enumerate(transcripts, 1):
                result = uc_analyze_transcript(
                    t, feat, base_context, solution_prompt, qc_threshold,
                    counter=f"[{i}/{len(transcripts)}]",
                )
                if result:
                    analyses.append(result)

        if analyses:
            summary = uc_summarize_feature(feat, analyses, base_context, feat_num, feat_total)
            if summary:
                for uc in UC_NAMES:
                    if feat.in_scope(uc):
                        feature_summaries_by_uc[uc].append((feat.display_name, summary))

    if unmatched:
        print(f"\n=== Processing {len(unmatched)} unmatched folders ===")
        for i, folder in enumerate(unmatched, 1):
            synthetic = Feature(
                prefix="", arm_name=folder.name, siemens_name=folder.name,
                definition="No backlog feature mapping. Extract requirements, decisions, and open items.",
                uc0_scope="", uc1_scope="", uc2_scope="", sf_owner="", folder=folder,
            )
            transcripts = _find_transcripts(folder)
            print(f"\n  [{i}/{len(unmatched)}] Unmatched: {folder.name} — {len(transcripts)} transcript(s)")
            analyses = []
            if not summary_only:
                for j, t in enumerate(transcripts, 1):
                    result = uc_analyze_transcript(
                        t, synthetic, base_context, solution_prompt, qc_threshold,
                        counter=f"[{j}/{len(transcripts)}]",
                    )
                    if result:
                        analyses.append(result)
            if analyses:
                uc_summarize_feature(synthetic, analyses, base_context, i, len(unmatched))

    print("\n=== Generating UC summaries ===")
    for uc, summaries in feature_summaries_by_uc.items():
        if summaries:
            uc_summarize_uc(uc, summaries, base_context)
        else:
            print(f"  [skip] {uc} — no feature summaries available")

    print("\n=== Generating gap report ===")
    generate_gap_report(registry, unmatched)

    print("\nAll done.")


# ===========================================================================
# BU Pipeline main()
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Siemens transcripts by BU using GitHub Copilot CLI")
    parser.add_argument("--uc", action="store_true",
                        help="Run the UC pipeline (maps folders to backlog features, produces gap report)")
    parser.add_argument("--bu", help="BU pipeline: process only this BU subfolder name")
    parser.add_argument("--feature", help="UC pipeline: process only this feature (e.g. '1.04')")
    parser.add_argument("--summary-only", action="store_true",
                        help="Skip transcript analysis; re-run summaries from existing [ANALYZED] files")
    parser.add_argument("--transcript-only", action="store_true",
                        help="Analyze each transcript and write [ANALYZED] files without generating BU summaries")
    parser.add_argument("--gap-only", action="store_true",
                        help="UC pipeline only: regenerate gap report without any API calls")
    parser.add_argument("--qc-only", action="store_true",
                        help="Only generate transcript quality report (no API calls)")
    parser.add_argument("--qc-threshold", type=int, default=QC_THRESHOLD_DEFAULT,
                        help="Minimum QC score required before transcript analysis runs; 0 disables gating")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the startup Copilot CLI connectivity/auth check (not recommended)")
    args = parser.parse_args()

    if args.qc_threshold < 0 or args.qc_threshold > 100:
        sys.exit("--qc-threshold must be between 0 and 100")

    # Route to UC pipeline when --uc is set
    if args.uc or args.gap_only or args.feature:
        run_uc_pipeline(
            summary_only=args.summary_only,
            gap_only=args.gap_only,
            qc_only=args.qc_only,
            feature_filter=args.feature,
            qc_threshold=args.qc_threshold,
        )
        return

    # BU pipeline
    setup_run_logging()

    if not TRANSCRIPTS_PATH_RAW:
        sys.exit("TRANSCRIPTS_PATH is missing. Set it in .env")

    if not TRANSCRIPTS_PATH.exists():
        sys.exit(f"TRANSCRIPTS_PATH not found: {TRANSCRIPTS_PATH}")

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)

    solution_prompt = load_solution_prompt()
    system = build_system_prompt(solution_prompt)

    bus = discover_bus(TRANSCRIPTS_PATH)
    if not bus:
        sys.exit(f"No .vtt or .txt files found under {TRANSCRIPTS_PATH}")

    if args.qc_only:
        generate_qc_report(bus)
        return

    if not args.skip_preflight:
        error = preflight_check()
        if error:
            sys.exit(
                "\n[FATAL] Cannot start the analysis run:\n\n"
                f"{error}\n\n"
                "Nothing has been processed yet — it's safe to fix this and re-run."
            )

    total_transcripts = sum(len(v) for v in bus.values())
    print(f"Found {total_transcripts} transcripts across {len(bus)} BUs: {', '.join(sorted(bus))}")

    if args.bu:
        if args.bu not in bus:
            sys.exit(f"BU '{args.bu}' not found. Available: {', '.join(sorted(bus))}")
        _progress["total"] = len(bus[args.bu])
        process_bu(
            args.bu,
            bus[args.bu],
            system,
            summary_only=args.summary_only,
            transcript_only=args.transcript_only,
            qc_threshold=args.qc_threshold,
        )
    else:
        bu_names = sorted(bus)
        bu_total = len(bu_names)
        _progress["total"] = total_transcripts
        for i, bu_name in enumerate(bu_names, 1):
            print(f"\n[{i}/{bu_total}] BU: {bu_name}")
            try:
                process_bu(
                    bu_name,
                    bus[bu_name],
                    system,
                    summary_only=args.summary_only,
                    transcript_only=args.transcript_only,
                    qc_threshold=args.qc_threshold,
                )
            except Exception as e:
                print(f"\n[ERROR] BU '{bu_name}' failed unexpectedly: {e}", file=sys.stderr)
                log_needs_followup(bu_name, "[BU]", f"UNEXPECTED BU ERROR: {e}")

    print("\nAll done.")


if __name__ == "__main__":
    main()
