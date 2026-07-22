"""
Client transcript analysis tool.

Walks TRANSCRIPTS_PATH for .vtt files organized by BU subfolder,
converts each to clean text, analyzes with Claude via GitHub Models API,
and produces per-BU summary files in OUTPUT_PATH.

Usage:
    python analyze.py                  # process all BUs
    python analyze.py --bu "Finance"   # single BU
    python analyze.py --summary-only   # re-run BU summaries from existing analyses
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
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

# Model tiers — Haiku for per-transcript (low-tier, high rate limit), Sonnet for summaries
MODEL_TRANSCRIPT = os.environ.get("MODEL_TRANSCRIPT", "claude-3-5-haiku-20241022")
MODEL_SUMMARY = os.environ.get("MODEL_SUMMARY", "claude-3-5-sonnet-20241022")

# Seconds to sleep between API calls to stay under rate limits
RATE_LIMIT_SLEEP = float(os.environ.get("RATE_LIMIT_SLEEP", "2"))
QC_THRESHOLD_DEFAULT = int(os.environ.get("QC_THRESHOLD", "0"))

# ---------------------------------------------------------------------------
# API client
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


def build_system_prompt(transcript_prompt: str) -> str:
    brief = _load_file("program_brief.txt")
    rolodex = _load_file("rolodex.txt")
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
    lines = vtt_path.read_text(encoding="utf-8", errors="replace").splitlines()
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
    return path.read_text(encoding="utf-8", errors="replace").strip()


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
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_meta(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


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

def analyze_transcript(vtt: Path, bu: str, system: str, qc_threshold: int = 0) -> str | None:
    out = analyzed_path(vtt, bu)
    meta_out = analyzed_meta_path(vtt, bu)
    source_sig = _source_signature(vtt)
    system_sig = _system_signature(system)
    meta = _read_meta(meta_out)
    if out.exists():
        if meta and _is_analysis_fresh(meta, source_sig, system_sig):
            print(f"  [skip] {vtt.name} — already analyzed (fresh cache)")
            return out.read_text(encoding="utf-8")
        print(f"  [reprocess] {vtt.name} — source/prompt/model changed or legacy cache")
    elif meta and qc_threshold > 0 and _is_qc_block_fresh(meta, source_sig, system_sig, qc_threshold):
        print(f"  [skip] {vtt.name} — blocked by QC threshold ({qc_threshold})")
        return None

    print(f"  [analyze] {vtt.name} ...", end=" ", flush=True)
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

        result = call_model(
            system=system,
            user=f"Transcript file: {vtt.name}\n\n{text}",
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
        return None


def summarize_bu(bu: str, analyses: list[str], system: str) -> None:
    out = summary_path(bu)
    print(f"  [summarize] {bu} ({len(analyses)} analyses) ...", end=" ", flush=True)

    bundle = "\n\n---\n\n".join(
        f"### Analysis {i + 1}\n{a}" for i, a in enumerate(analyses)
    )

    prompt = (
        f"The following are individual analyses of discovery/design call transcripts "
        f"from the **{bu}** business unit.\n\n"
        f"Synthesize across all of them:\n"
        f"1. **Confirmed Requirements** — explicitly or repeatedly stated requirements\n"
        f"2. **Open Decisions** — items discussed but not resolved across the calls\n"
        f"3. **Stated Constraints** — technical, organizational, or timeline constraints\n"
        f"4. **Key Themes** — recurring topics or concerns\n"
        f"5. **Named Stakeholders** — who appeared and what positions/concerns they expressed\n"
        f"6. **Top 3 Unknowns** — the highest-priority gaps that need resolution before design can proceed\n\n"
        f"---\n\n{bundle}"
    )

    try:
        result = call_model(system=system, user=prompt, model=MODEL_SUMMARY, max_tokens=8192)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result, encoding="utf-8")
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)


def process_bu(
    bu: str,
    vtts: list[Path],
    system: str,
    summary_only: bool = False,
    transcript_only: bool = False,
    qc_threshold: int = 0,
) -> None:
    print(f"\n=== BU: {bu} ({len(vtts)} transcripts) ===")
    analyses: list[str] = []

    if summary_only:
        # Load existing analyses
        for vtt in vtts:
            out = analyzed_path(vtt, bu)
            if out.exists():
                analyses.append(out.read_text(encoding="utf-8"))
            else:
                print(f"  [warn] no analysis found for {vtt.name} — skipping from summary")
    else:
        for vtt in vtts:
            result = analyze_transcript(vtt, bu, system, qc_threshold=qc_threshold)
            if result:
                analyses.append(result)

    if transcript_only:
        print(f"  [skip summary] transcript-only mode for {bu}")
        return

    if analyses:
        summarize_bu(bu, analyses, system)
    else:
        print(f"  [warn] no analyses available for {bu}, skipping summary")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Siemens VTT transcripts by BU")
    parser.add_argument("--bu", help="Process only this BU subfolder name")
    parser.add_argument("--summary-only", action="store_true",
                        help="Skip transcript analysis; re-run BU summaries from existing [ANALYZED] files")
    parser.add_argument("--transcript-only", action="store_true",
                        help="Analyze each transcript and write [ANALYZED] files without generating BU summaries")
    parser.add_argument("--qc-only", action="store_true",
                        help="Only generate transcript quality report (no API calls)")
    parser.add_argument("--qc-threshold", type=int, default=QC_THRESHOLD_DEFAULT,
                        help="Minimum QC score required before transcript analysis runs; 0 disables gating")
    args = parser.parse_args()

    if args.qc_threshold < 0 or args.qc_threshold > 100:
        sys.exit("--qc-threshold must be between 0 and 100")

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

    print(f"Found {sum(len(v) for v in bus.values())} transcripts across {len(bus)} BUs: {', '.join(sorted(bus))}")

    if args.bu:
        if args.bu not in bus:
            sys.exit(f"BU '{args.bu}' not found. Available: {', '.join(sorted(bus))}")
        process_bu(
            args.bu,
            bus[args.bu],
            system,
            summary_only=args.summary_only,
            transcript_only=args.transcript_only,
            qc_threshold=args.qc_threshold,
        )
    else:
        for bu_name in sorted(bus):
            process_bu(
                bu_name,
                bus[bu_name],
                system,
                summary_only=args.summary_only,
                transcript_only=args.transcript_only,
                qc_threshold=args.qc_threshold,
            )

    print("\nAll done.")


if __name__ == "__main__":
    main()
