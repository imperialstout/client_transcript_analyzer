"""
Use-case focused transcript analysis pipeline.

Reads the three backlog CSVs (UC0, UC1, UC2) to build a feature registry,
then walks TRANSCRIPTS_PATH for feature-named subfolders (.vtt or .txt files),
analyzes each transcript with feature context injected, and produces:

  output/
    features/
      <feature_folder>/
        <transcript> [ANALYZED].txt    — per-transcript analysis
      [FEATURE SUMMARY] <feature>.md   — synthesized across all transcripts in that folder
    [UC SUMMARY] UC0.md
    [UC SUMMARY] UC1.md
    [UC SUMMARY] UC2.md
    [GAP REPORT] coverage.md           — which backlog rows have no transcript coverage

Usage:
    python analyze_uc.py                        # full pipeline
    python analyze_uc.py --feature "1.04"       # single feature folder (by numeric prefix)
    python analyze_uc.py --summary-only         # re-run summaries from existing [ANALYZED] files
    python analyze_uc.py --gap-only             # just regenerate the gap report
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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from dotenv import load_dotenv
from openai import OpenAI

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
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
CONTEXT_DIR = Path(__file__).parent / "client_context"

MODEL_TRANSCRIPT = os.environ.get("MODEL_TRANSCRIPT", "claude-3-5-haiku-20241022")
MODEL_SUMMARY = os.environ.get("MODEL_SUMMARY", "claude-3-5-sonnet-20241022")
RATE_LIMIT_SLEEP = float(os.environ.get("RATE_LIMIT_SLEEP", "2"))
QC_THRESHOLD_DEFAULT = int(os.environ.get("QC_THRESHOLD", "0"))

# Backlog CSVs — paths relative to this script
_HERE = Path(__file__).parent
CSV_UC0 = _HERE / "client_data" / "20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - Epic UC0.csv"
CSV_UC1 = _HERE / "client_data" / "20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - Epic UC 1.csv"
CSV_UC2 = _HERE / "client_data" / "20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - EpicUC02 Technician Quote.csv"

UC_NAMES = {
    "UC0": "BX SaaS Quote (Use Case 0)",
    "UC1": "SOLSYS End-to-End Quote (Use Case 1)",
    "UC2": "SNGX Technician Quote (Use Case 2)",
}


def required_csv_paths() -> list[Path]:
    return [CSV_UC0, CSV_UC1, CSV_UC2]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Feature:
    prefix: str              # e.g. "1.04"
    arm_name: str            # ARM internal name
    siemens_name: str        # Siemens epic name (may be blank or "NA")
    definition: str
    uc0_scope: str           # raw value from CSV column
    uc1_scope: str
    uc2_scope: str
    sf_owner: str = ""
    folder: Path | None = None          # matched transcript folder (if any)

    @property
    def display_name(self) -> str:
        return self.siemens_name if self.siemens_name and self.siemens_name.upper() != "NA" else self.arm_name

    def in_scope(self, uc: str) -> bool:
        val = {"UC0": self.uc0_scope, "UC1": self.uc1_scope, "UC2": self.uc2_scope}.get(uc, "")
        return bool(val) and val.upper() not in ("NO", "NOT APPLICABLE", "N/A", "")

    def scope_label(self, uc: str) -> str:
        val = {"UC0": self.uc0_scope, "UC1": self.uc1_scope, "UC2": self.uc2_scope}.get(uc, "")
        if not val or val.upper() in ("NO", "NOT APPLICABLE", "N/A"):
            return "✗"
        if val.upper() in ("YES",):
            return "✓"
        if "UNCLEAR" in val.upper():
            return "?"
        # Partial / conditional note
        return "~"


# ---------------------------------------------------------------------------
# API client (duplicated from analyze.py to stay self-contained)
# ---------------------------------------------------------------------------

_client: OpenAI | None = None


def _gh_executable() -> str:
    gh_path = shutil.which("gh")
    if gh_path:
        return gh_path

    candidates: list[str] = []
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles", "")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        user_profile = os.environ.get("USERPROFILE", "")
        candidates.extend([
            os.path.join(program_files, "GitHub CLI", "gh.exe"),
            os.path.join(local_app_data, "Programs", "GitHub CLI", "gh.exe"),
            os.path.join(user_profile, "AppData", "Local", "Microsoft", "WinGet", "Links", "gh.exe"),
        ])

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate

    return "gh"


def _resolve_github_token() -> str:
    if GITHUB_TOKEN:
        return GITHUB_TOKEN

    try:
        result = subprocess.run(
            [_gh_executable(), "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "GitHub authentication is missing. Set GITHUB_TOKEN in .env or install GitHub CLI and run 'gh auth login'."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "GitHub authentication is missing. Set GITHUB_TOKEN in .env or run 'gh auth login'."
        ) from exc

    token = result.stdout.strip()
    if not token:
        raise RuntimeError(
            "GitHub authentication is missing. Set GITHUB_TOKEN in .env or run 'gh auth login'."
        )
    return token


def _api() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=_resolve_github_token(),
        )
    return _client


def call_model(system: str, user: str, model: str, max_tokens: int = 4096) -> str:
    response = _api().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------

def _load_file(name: str) -> str:
    path = CONTEXT_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def load_base_context() -> str:
    brief = _load_file("program_brief.txt")
    rolodex = _load_file("rolodex.txt")
    parts = []
    if brief:
        parts.append(brief)
    if rolodex:
        parts.append("## People Reference\n" + rolodex)
    return "\n\n---\n\n".join(parts)


def load_solution_prompt() -> str:
    """Load optional transcript extraction guidance used by the BU pipeline."""
    return _load_file("solution_prompt.txt")


# ---------------------------------------------------------------------------
# VTT parser (duplicated from analyze.py)
# ---------------------------------------------------------------------------

_VTT_SKIP = re.compile(
    r"^(WEBVTT|NOTE|STYLE|REGION)"
    r"|^\d+$"
    r"|^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->"
    r"|^$",
    re.IGNORECASE,
)
_SPEAKER_LINE = re.compile(r"^([^:]{2,60}):\s+(.+)$")


def vtt_to_text(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    segments: list[tuple[str, str]] = []
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
            current_text.append(clean)

    flush()

    deduped: list[tuple[str, str]] = []
    for seg in segments:
        if not deduped or seg != deduped[-1]:
            deduped.append(seg)

    return "\n".join(f"{spk}: {txt}" if spk else txt for spk, txt in deduped)


def transcript_to_text(path: Path) -> str:
    """Parse .vtt or .txt transcript to clean speaker-prefixed text."""
    if path.suffix.lower() == ".vtt":
        return vtt_to_text(path)
    # Plain .txt — read as-is; Teams exports are already human-readable
    return path.read_text(encoding="utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# CSV parsing → feature registry
# ---------------------------------------------------------------------------

_PREFIX_RE = re.compile(r"^(\d+\.\d+)")


def _extract_prefix(name: str) -> str:
    """Extract numeric prefix like '1.04' from a name or folder."""
    m = _PREFIX_RE.match(name.strip())
    return m.group(1) if m else ""


def _normalize_scope(val: str) -> str:
    return (val or "").strip()


def load_feature_registry() -> dict[str, Feature]:
    """
    Parse all 3 CSVs and return a registry keyed by numeric prefix.
    Features without a Siemens numeric name are keyed by ARM name slug.
    When the same feature appears in multiple CSVs the UC scope columns are merged.
    """
    registry: dict[str, Feature] = {}

    def _key(arm_name: str, siemens_name: str) -> str:
        p = _extract_prefix(siemens_name)
        if p:
            return p
        # Fall back to normalised ARM name as key
        return re.sub(r"[^a-z0-9]+", "-", arm_name.lower().strip()).strip("-")

    def _parse(csv_path: Path, uc_col: str, uc_attr: str) -> None:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                arm = row.get("Epic / Capability Name(ARM)", "").strip()
                siemens = row.get("Epic / Capability Name(Siemens)", "").strip()
                definition = row.get("Definition", "").strip()
                scope_val = _normalize_scope(row.get(uc_col, ""))
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
                        uc0_scope="",
                        uc1_scope="",
                        uc2_scope="",
                        sf_owner=owner,
                    )
                else:
                    # Update definition if currently blank
                    if not registry[key].definition and definition:
                        registry[key].definition = definition
                    if not registry[key].sf_owner and owner:
                        registry[key].sf_owner = owner

                setattr(registry[key], uc_attr, scope_val)

    _parse(CSV_UC0, "UC-00", "uc0_scope")
    _parse(CSV_UC1, "UC-01 SOLSYS E2E Quote", "uc1_scope")
    _parse(CSV_UC2, "UC-02 SNGX Technician", "uc2_scope")

    return registry


# ---------------------------------------------------------------------------
# Folder → feature matching
# ---------------------------------------------------------------------------

def match_folders_to_features(registry: dict[str, Feature]) -> list[Path]:
    """
    Walk TRANSCRIPTS_PATH immediate subdirs, match each to a feature by numeric prefix.
    Returns list of unmatched folders (for warning output).
    """
    unmatched: list[Path] = []

    for folder in sorted(TRANSCRIPTS_PATH.iterdir()):
        if not folder.is_dir():
            continue

        prefix = _extract_prefix(folder.name)
        if prefix and prefix in registry:
            registry[prefix].folder = folder
        elif prefix:
            # Prefix found but no exact match — try loose match
            matched = False
            for key, feat in registry.items():
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
# Output paths
# ---------------------------------------------------------------------------

_FEATURES_OUT = OUTPUT_PATH / "features"


def analyzed_path(transcript: Path, feature_key: str) -> Path:
    return _FEATURES_OUT / feature_key / f"{transcript.stem} [ANALYZED].txt"


def analyzed_meta_path(transcript: Path, feature_key: str) -> Path:
    return _FEATURES_OUT / feature_key / f"{transcript.stem} [ANALYZED].meta.json"


def feature_summary_path(feature_key: str, display_name: str) -> Path:
    safe = re.sub(r'[<>:"/\\|?*]', "-", display_name)
    return _FEATURES_OUT / feature_key / f"[FEATURE SUMMARY] {safe}.md"


def uc_summary_path(uc: str) -> Path:
    return OUTPUT_PATH / f"[UC SUMMARY] {uc}.md"


def gap_report_path() -> Path:
    return OUTPUT_PATH / "[GAP REPORT] coverage.md"


def qc_report_path() -> Path:
    return OUTPUT_PATH / "[QC REPORT] transcript_quality.md"


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


def _context_signature(base_context: str, solution_prompt: str) -> str:
    return _sha256_bytes((base_context + "\n\n---\n\n" + solution_prompt).encode("utf-8"))


def _read_meta(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _is_analysis_fresh(meta: dict[str, object], source_sig: dict[str, object], context_sig: str) -> bool:
    required = {
        "status": "ok",
        "source_sha256": source_sig["source_sha256"],
        "source_size": source_sig["source_size"],
        "source_mtime_ns": source_sig["source_mtime_ns"],
        "model_transcript": MODEL_TRANSCRIPT,
        "context_signature": context_sig,
    }
    return all(meta.get(k) == v for k, v in required.items())


def _is_qc_block_fresh(
    meta: dict[str, object],
    source_sig: dict[str, object],
    context_sig: str,
    qc_threshold: int,
) -> bool:
    required = {
        "status": "qc_blocked",
        "source_sha256": source_sig["source_sha256"],
        "source_size": source_sig["source_size"],
        "source_mtime_ns": source_sig["source_mtime_ns"],
        "model_transcript": MODEL_TRANSCRIPT,
        "context_signature": context_sig,
        "qc_threshold": qc_threshold,
    }
    return all(meta.get(k) == v for k, v in required.items())


def _write_meta(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _iter_pipeline_transcripts(registry: dict[str, Feature], unmatched_folders: list[Path]) -> list[tuple[Path, str]]:
    """Collect transcripts exactly as this pipeline discovers them."""
    items: list[tuple[Path, str]] = []

    for feat in registry.values():
        if feat.folder is None:
            continue
        key = feat.prefix or re.sub(r"[^a-z0-9]+", "-", feat.arm_name.lower())
        for p in find_transcripts(feat.folder):
            items.append((p, key))

    for folder in unmatched_folders:
        key = re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-") or "unmatched"
        for p in find_transcripts(folder):
            items.append((p, key))

    for p in sorted(TRANSCRIPTS_PATH.iterdir()):
        if p.is_file() and p.suffix.lower() in (".vtt", ".txt"):
            items.append((p, "ROOT"))

    return items


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


def generate_qc_report(registry: dict[str, Feature], unmatched_folders: list[Path]) -> None:
    out = qc_report_path()
    out.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    for transcript, bucket in _iter_pipeline_transcripts(registry, unmatched_folders):
        try:
            text = transcript_to_text(transcript)
            metrics = _quality_metrics(text)
            score, label, flags = _quality_assessment(metrics)
            rows.append({
                "path": str(transcript),
                "bucket": bucket,
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
                "bucket": bucket,
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
# Prompts
# ---------------------------------------------------------------------------

def build_transcript_system_prompt(feat: Feature, base_context: str, solution_prompt: str) -> str:
    uc_scope_parts = []
    for uc in ("UC0", "UC1", "UC2"):
        if feat.in_scope(uc):
            uc_scope_parts.append(f"{uc} ({UC_NAMES[uc]})")

    uc_scope = ", ".join(uc_scope_parts) if uc_scope_parts else "scope TBD"

    feature_block = f"""## Analysis Focus: {feat.display_name}

You are analyzing a discovery or design call transcript specifically for the **{feat.display_name}** capability.

**Feature Definition:**
{feat.definition or "(no definition available)"}

**In scope for:** {uc_scope}

Extract only information relevant to this feature. Structure your output as:

1. **Requirements Confirmed** — explicit functional or non-functional requirements discussed for this feature
2. **Decisions Made** — design or scoping decisions that were confirmed or agreed
3. **Open Items** — unresolved questions or TBD decisions about this feature
4. **Constraints** — technical, timeline, or organizational constraints that affect this feature
5. **Coverage Gaps** — things that should have been discussed for this feature but were not
6. **Key Participants** — who spoke about this feature and their stated position or concern
7. **Private Read** — candid assessment: confidence level in what was discussed, any political subtext or risks

If this transcript has no meaningful discussion of **{feat.display_name}**, say so explicitly in one sentence and stop."""

    parts = []
    if base_context:
        parts.append(base_context)
    if solution_prompt:
        parts.append("## Global Transcript Extraction Guidance\n" + solution_prompt)
    parts.append(feature_block)
    return "\n\n---\n\n".join(parts)


def build_feature_summary_prompt(feat: Feature, analyses: list[str]) -> str:
    uc_scope = ", ".join(uc for uc in ("UC0", "UC1", "UC2") if feat.in_scope(uc)) or "TBD"
    bundle = "\n\n---\n\n".join(f"### Transcript Analysis {i+1}\n{a}" for i, a in enumerate(analyses))

    return f"""The following are {len(analyses)} transcript analyses for the **{feat.display_name}** capability (in scope: {uc_scope}).

Feature definition:
{feat.definition or "(no definition available)"}

Synthesize across all analyses:

1. **Confirmed Scope** — what's definitively in or out of scope for this feature
2. **Key Decisions** — design choices that have been agreed
3. **Open Items** — questions still unresolved across all calls
4. **Coverage Confidence** — how well do the transcripts actually cover this feature? What's still dark?
5. **Top 3 Unknowns** — highest-priority gaps that must be resolved before design can proceed
6. **Owner / Stakeholders** — who owns this feature and who has strong opinions about it

---

{bundle}"""


def build_uc_summary_prompt(uc: str, feature_summaries: list[tuple[str, str]]) -> str:
    feature_list = "\n".join(f"- {name}" for name, _ in feature_summaries)
    bundle = "\n\n---\n\n".join(
        f"### {name}\n{summary}" for name, summary in feature_summaries
    )

    return f"""The following are feature summaries for **{UC_NAMES[uc]}**.

Features covered:
{feature_list}

Synthesize across all features for this use case:

1. **UC Scope Summary** — what this use case is trying to accomplish end-to-end
2. **Confirmed Design Decisions** — locked choices across features
3. **Cross-Feature Dependencies** — where features interact or sequence matters
4. **Top Open Items** — highest-priority unresolved items across the UC
5. **Coverage Gaps** — in-scope features with weak or no transcript coverage; what's not understood
6. **Readiness Assessment** — overall confidence we understand this UC well enough to start HLD

---

{bundle}"""


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def analyze_transcript(
    transcript: Path,
    feat: Feature,
    base_context: str,
    solution_prompt: str,
    qc_threshold: int = 0,
) -> str | None:
    feature_key = feat.prefix or re.sub(r"[^a-z0-9]+", "-", feat.arm_name.lower())
    out = analyzed_path(transcript, feature_key)
    meta_out = analyzed_meta_path(transcript, feature_key)
    source_sig = _source_signature(transcript)
    context_sig = _context_signature(base_context, solution_prompt)
    meta = _read_meta(meta_out)

    if out.exists():
        if meta and _is_analysis_fresh(meta, source_sig, context_sig):
            print(f"    [skip] {transcript.name} — already analyzed (fresh cache)")
            return out.read_text(encoding="utf-8")
        print(f"    [reprocess] {transcript.name} — source/prompt/model changed or legacy cache")
    elif meta and qc_threshold > 0 and _is_qc_block_fresh(meta, source_sig, context_sig, qc_threshold):
        print(f"    [skip] {transcript.name} — blocked by QC threshold ({qc_threshold})")
        return None

    print(f"    [analyze] {transcript.name} ...", end=" ", flush=True)
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
                "context_signature": context_sig,
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
                "context_signature": context_sig,
                "updated_at_utc": datetime.now(UTC).isoformat(),
            })
            return None

        system = build_transcript_system_prompt(feat, base_context, solution_prompt)
        result = call_model(
            system=system,
            user=f"Transcript: {transcript.name}\n\n{text}",
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
            "context_signature": context_sig,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        })
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
        return result
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        _write_meta(meta_out, {
            **source_sig,
            "status": "error",
            "error": str(e),
            "qc_threshold": qc_threshold,
            "model_transcript": MODEL_TRANSCRIPT,
            "context_signature": context_sig,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        })
        return None


def summarize_feature(feat: Feature, analyses: list[str], base_context: str) -> str | None:
    feature_key = feat.prefix or re.sub(r"[^a-z0-9]+", "-", feat.arm_name.lower())
    out = feature_summary_path(feature_key, feat.display_name)
    if out.exists():
        print(f"  [skip summary] {feat.display_name}")
        return out.read_text(encoding="utf-8")

    print(f"  [feature summary] {feat.display_name} ({len(analyses)} analyses) ...", end=" ", flush=True)
    prompt = build_feature_summary_prompt(feat, analyses)
    try:
        result = call_model(system=base_context or "You are an expert Salesforce Revenue Cloud architect.", user=prompt, model=MODEL_SUMMARY, max_tokens=4096)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result, encoding="utf-8")
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
        return result
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None


def summarize_uc(uc: str, feature_summaries: list[tuple[str, str]], base_context: str) -> None:
    out = uc_summary_path(uc)
    if out.exists():
        print(f"  [skip] {uc} summary already exists — delete to regenerate")
        return

    print(f"  [UC summary] {uc} ({len(feature_summaries)} features) ...", end=" ", flush=True)
    if not feature_summaries:
        print("SKIP (no feature summaries)")
        return

    prompt = build_uc_summary_prompt(uc, feature_summaries)
    try:
        result = call_model(system=base_context or "You are an expert Salesforce Revenue Cloud architect.", user=prompt, model=MODEL_SUMMARY, max_tokens=8192)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result, encoding="utf-8")
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Gap report (no API call — pure data)
# ---------------------------------------------------------------------------

def generate_gap_report(registry: dict[str, Feature], unmatched_folders: list[Path]) -> None:
    out = gap_report_path()
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    features = list(registry.values())
    with_folder = [f for f in features if f.folder is not None]
    without_folder = [f for f in features if f.folder is None]

    # Count transcript files per feature
    def transcript_count(feat: Feature) -> int:
        if feat.folder is None:
            return 0
        return sum(1 for p in feat.folder.iterdir()
                   if p.is_file() and p.suffix.lower() in (".vtt", ".txt"))

    def analysis_count(feat: Feature) -> int:
        key = feat.prefix or re.sub(r"[^a-z0-9]+", "-", feat.arm_name.lower())
        d = _FEATURES_OUT / key
        if not d.exists():
            return 0
        return sum(1 for p in d.iterdir() if "[ANALYZED]" in p.name)

    lines.append("# Coverage Gap Report\n")
    lines.append(f"Total features in backlog: **{len(features)}**  ")
    lines.append(f"Features with transcript folders: **{len(with_folder)}**  ")
    lines.append(f"Features without transcript folders: **{len(without_folder)}**\n")

    # Summary table
    lines.append("## Coverage Matrix\n")
    lines.append("| Feature | ARM Capability | UC0 | UC1 | UC2 | Transcripts | Analyzed |")
    lines.append("|---------|---------------|-----|-----|-----|-------------|----------|")

    for feat in sorted(features, key=lambda f: (f.prefix or "z", f.arm_name)):
        tc = transcript_count(feat)
        ac = analysis_count(feat)
        status = f"{ac}/{tc}" if tc else "**NO COVERAGE**"
        row = (
            f"| {feat.prefix or '—'} "
            f"| {feat.arm_name} "
            f"| {feat.scope_label('UC0')} "
            f"| {feat.scope_label('UC1')} "
            f"| {feat.scope_label('UC2')} "
            f"| {tc} "
            f"| {status} |"
        )
        lines.append(row)

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
                # First sentence of definition as context
                first_sentence = feat.definition.split(".")[0].strip()
                lines.append(f"  _{first_sentence}_")
    else:
        lines.append("_All in-scope features have at least one transcript folder._")

    # Unmatched folders warning
    if unmatched_folders:
        lines.append("\n## Unmatched Folders (not in backlog CSVs)\n")
        lines.append("These folders were found but could not be matched to a backlog feature:\n")
        for p in unmatched_folders:
            lines.append(f"- `{p.name}`")

    # Not-in-scope features (informational)
    not_in_scope = [f for f in features if not any(f.in_scope(uc) for uc in ("UC0", "UC1", "UC2"))]
    if not_in_scope:
        lines.append("\n## Out-of-Scope Features (no UC coverage required)\n")
        for feat in sorted(not_in_scope, key=lambda f: f.prefix or "z"):
            lines.append(f"- {feat.prefix or '—'} {feat.arm_name}")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [gap report] written → {out}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def find_transcripts(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".vtt", ".txt")
    )


def process_feature(
    feat: Feature,
    base_context: str,
    solution_prompt: str,
    summary_only: bool = False,
    qc_threshold: int = 0,
) -> str | None:
    folder_name = feat.folder.name if feat.folder else "(no folder)"
    transcripts = find_transcripts(feat.folder) if feat.folder else []
    print(f"\n  Feature: {feat.display_name} [{folder_name}] — {len(transcripts)} transcripts")

    analyses: list[str] = []

    if summary_only:
        feature_key = feat.prefix or re.sub(r"[^a-z0-9]+", "-", feat.arm_name.lower())
        d = _FEATURES_OUT / feature_key
        if d.exists():
            for p in sorted(d.iterdir()):
                if "[ANALYZED]" in p.name:
                    analyses.append(p.read_text(encoding="utf-8"))
        if not analyses:
            print(f"    [warn] no existing analyses for {feat.display_name}")
            return None
    else:
        if not transcripts:
            print(f"    [skip] no transcripts")
            return None
        for t in transcripts:
            result = analyze_transcript(t, feat, base_context, solution_prompt, qc_threshold=qc_threshold)
            if result:
                analyses.append(result)

    if not analyses:
        return None

    return summarize_feature(feat, analyses, base_context)


def process_unmatched_folder(
    folder: Path,
    base_context: str,
    solution_prompt: str,
    summary_only: bool = False,
    qc_threshold: int = 0,
) -> str | None:
    """Analyze folders not mapped to backlog features using generic context."""
    synthetic_feature = Feature(
        prefix="",
        arm_name=folder.name,
        siemens_name=folder.name,
        definition=(
            "No backlog feature mapping found for this folder. "
            "Extract the strongest requirements, decisions, and open items that appear in this transcript."
        ),
        uc0_scope="",
        uc1_scope="",
        uc2_scope="",
        sf_owner="",
        folder=folder,
    )
    print(f"\n  Unmatched folder: {folder.name}")
    return process_feature(
        synthetic_feature,
        base_context,
        solution_prompt,
        summary_only=summary_only,
        qc_threshold=qc_threshold,
    )


def process_root_transcripts(
    base_context: str,
    solution_prompt: str,
    summary_only: bool = False,
    qc_threshold: int = 0,
) -> str | None:
    """Analyze transcript files that exist directly under TRANSCRIPTS_PATH."""
    synthetic_feature = Feature(
        prefix="",
        arm_name="ROOT",
        siemens_name="ROOT",
        definition=(
            "Transcript found directly in TRANSCRIPTS_PATH without a feature folder. "
            "Extract the strongest requirements, decisions, and open items."
        ),
        uc0_scope="",
        uc1_scope="",
        uc2_scope="",
        sf_owner="",
        folder=TRANSCRIPTS_PATH,
    )

    root_transcripts = [
        p for p in sorted(TRANSCRIPTS_PATH.iterdir())
        if p.is_file() and p.suffix.lower() in (".vtt", ".txt")
    ]

    if not root_transcripts:
        return None

    print(f"\n=== Processing root-level transcripts ({len(root_transcripts)}) ===")

    if summary_only:
        return process_feature(
            synthetic_feature,
            base_context,
            solution_prompt,
            summary_only=True,
            qc_threshold=qc_threshold,
        )

    analyses: list[str] = []
    for t in root_transcripts:
        result = analyze_transcript(
            t,
            synthetic_feature,
            base_context,
            solution_prompt,
            qc_threshold=qc_threshold,
        )
        if result:
            analyses.append(result)

    if not analyses:
        return None

    return summarize_feature(synthetic_feature, analyses, base_context)


def main() -> None:
    parser = argparse.ArgumentParser(description="Use-case focused transcript analysis")
    parser.add_argument("--feature", help="Process only this feature (numeric prefix, e.g. '1.04')")
    parser.add_argument("--summary-only", action="store_true",
                        help="Skip transcript analysis; re-run summaries from existing [ANALYZED] files")
    parser.add_argument("--gap-only", action="store_true",
                        help="Only regenerate the gap report (no API calls)")
    parser.add_argument("--qc-only", action="store_true",
                        help="Only generate transcript quality report (no API calls)")
    parser.add_argument("--qc-threshold", type=int, default=QC_THRESHOLD_DEFAULT,
                        help="Minimum QC score required before transcript analysis runs; 0 disables gating")
    args = parser.parse_args()

    if args.qc_threshold < 0 or args.qc_threshold > 100:
        sys.exit("--qc-threshold must be between 0 and 100")

    setup_run_logging()

    if not TRANSCRIPTS_PATH_RAW:
        sys.exit("TRANSCRIPTS_PATH is missing. Set it in ~/.config/client-transcript-analyzer/.env")

    if not TRANSCRIPTS_PATH.exists():
        sys.exit(f"TRANSCRIPTS_PATH not found: {TRANSCRIPTS_PATH}")

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    _FEATURES_OUT.mkdir(parents=True, exist_ok=True)

    missing_csvs = [p for p in required_csv_paths() if not p.exists()]

    if missing_csvs and not args.qc_only:
        missing = "\n".join(f"- {p}" for p in missing_csvs)
        sys.exit(
            "Required backlog CSV files are missing.\n"
            "Place them under client_data/ or run with --qc-only.\n"
            f"Missing files:\n{missing}"
        )

    if missing_csvs and args.qc_only:
        print("[warn] Backlog CSV files missing; QC will run folder-only discovery.")
        registry = {}
        unmatched = [p for p in sorted(TRANSCRIPTS_PATH.iterdir()) if p.is_dir()]
        matched = 0
    else:
        print("Loading feature registry from backlog CSVs...")
        registry = load_feature_registry()
        print(f"  {len(registry)} features loaded")

        print("Matching transcript folders...")
        unmatched = match_folders_to_features(registry)
        matched = sum(1 for f in registry.values() if f.folder is not None)
        print(f"  {matched} folders matched, {len(unmatched)} unmatched")
        if unmatched:
            print(f"  Unmatched: {[p.name for p in unmatched]}")

    if args.gap_only:
        generate_gap_report(registry, unmatched)
        return

    if args.qc_only:
        generate_qc_report(registry, unmatched)
        return

    base_context = load_base_context()
    solution_prompt = load_solution_prompt()

    if args.feature:
        feat = registry.get(args.feature)
        if not feat:
            sys.exit(f"Feature '{args.feature}' not found. Available prefixes: {sorted(registry)}")
        process_feature(
            feat,
            base_context,
            solution_prompt,
            summary_only=args.summary_only,
            qc_threshold=args.qc_threshold,
        )
        generate_gap_report(registry, unmatched)
        return

    # Full pipeline: analyze all features, then UC rollups, then gap report
    print(f"\n=== Processing {matched} features with transcripts ===")
    feature_summaries_by_uc: dict[str, list[tuple[str, str]]] = {uc: [] for uc in UC_NAMES}

    for key, feat in sorted(registry.items(), key=lambda kv: (kv[1].prefix or "z", kv[1].arm_name)):
        if feat.folder is None:
            continue
        summary = process_feature(
            feat,
            base_context,
            solution_prompt,
            summary_only=args.summary_only,
            qc_threshold=args.qc_threshold,
        )
        if summary:
            for uc in UC_NAMES:
                if feat.in_scope(uc):
                    feature_summaries_by_uc[uc].append((feat.display_name, summary))

    if unmatched:
        print(f"\n=== Processing {len(unmatched)} unmatched folders ===")
        for folder in unmatched:
            process_unmatched_folder(
                folder,
                base_context,
                solution_prompt,
                summary_only=args.summary_only,
                qc_threshold=args.qc_threshold,
            )

    process_root_transcripts(
        base_context,
        solution_prompt,
        summary_only=args.summary_only,
        qc_threshold=args.qc_threshold,
    )

    print("\n=== Generating UC summaries ===")
    for uc, summaries in feature_summaries_by_uc.items():
        if summaries:
            summarize_uc(uc, summaries, base_context)
        else:
            print(f"  [skip] {uc} — no feature summaries available")

    print("\n=== Generating gap report ===")
    generate_gap_report(registry, unmatched)

    print("\nAll done.")


if __name__ == "__main__":
    main()
