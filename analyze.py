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
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path.home() / ".config" / "client-transcript-analyzer" / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TRANSCRIPTS_PATH = Path(os.environ["TRANSCRIPTS_PATH"])
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", Path(__file__).parent / "output"))
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
CONTEXT_DIR = Path(__file__).parent / "client_context"

# Model tiers — Haiku for per-transcript (low-tier, high rate limit), Sonnet for summaries
MODEL_TRANSCRIPT = os.environ.get("MODEL_TRANSCRIPT", "claude-3-5-haiku-20241022")
MODEL_SUMMARY = os.environ.get("MODEL_SUMMARY", "claude-3-5-sonnet-20241022")

# Seconds to sleep between API calls to stay under rate limits
RATE_LIMIT_SLEEP = float(os.environ.get("RATE_LIMIT_SLEEP", "2"))

# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

_client: OpenAI | None = None


def _api() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=GITHUB_TOKEN,
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


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_bus(root: Path) -> dict[str, list[Path]]:
    """Return {bu_name: [vtt_paths]} grouped by immediate subfolder."""
    bus: dict[str, list[Path]] = {}
    for path in sorted(root.rglob("*.vtt")):
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


def summary_path(bu: str) -> Path:
    return OUTPUT_PATH / bu / f"[BU SUMMARY] {bu}.md"


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def analyze_transcript(vtt: Path, bu: str, system: str) -> str | None:
    out = analyzed_path(vtt, bu)
    if out.exists():
        print(f"  [skip] {vtt.name} — already analyzed")
        return out.read_text(encoding="utf-8")

    print(f"  [analyze] {vtt.name} ...", end=" ", flush=True)
    try:
        text = vtt_to_text(vtt)
        if len(text) < 100:
            print("SKIP (too short after VTT parse)")
            return None

        result = call_model(
            system=system,
            user=f"Transcript file: {vtt.name}\n\n{text}",
            model=MODEL_TRANSCRIPT,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result, encoding="utf-8")
        print("done")
        time.sleep(RATE_LIMIT_SLEEP)
        return result
    except Exception as e:
        print(f"ERROR: {e}")
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
        print(f"ERROR: {e}")


def process_bu(bu: str, vtts: list[Path], system: str, summary_only: bool = False) -> None:
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
            result = analyze_transcript(vtt, bu, system)
            if result:
                analyses.append(result)

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
    args = parser.parse_args()

    if not TRANSCRIPTS_PATH.exists():
        sys.exit(f"TRANSCRIPTS_PATH not found: {TRANSCRIPTS_PATH}")

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)

    solution_prompt = load_solution_prompt()
    system = build_system_prompt(solution_prompt)

    bus = discover_bus(TRANSCRIPTS_PATH)
    if not bus:
        sys.exit(f"No .vtt files found under {TRANSCRIPTS_PATH}")

    print(f"Found {sum(len(v) for v in bus.values())} transcripts across {len(bus)} BUs: {', '.join(sorted(bus))}")

    if args.bu:
        if args.bu not in bus:
            sys.exit(f"BU '{args.bu}' not found. Available: {', '.join(sorted(bus))}")
        process_bu(args.bu, bus[args.bu], system, summary_only=args.summary_only)
    else:
        for bu_name in sorted(bus):
            process_bu(bu_name, bus[bu_name], system, summary_only=args.summary_only)

    print("\nAll done.")


if __name__ == "__main__":
    main()
