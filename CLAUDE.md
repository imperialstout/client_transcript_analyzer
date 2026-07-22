# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

CLI tools for bulk-analyzing Teams meeting transcripts using Claude via the GitHub Copilot CLI. Designed to run on a Siemens-managed Windows VM where direct API access and GitHub PATs are not available — only GitHub Copilot CLI is.

## Active Scripts

| Script | Purpose |
|--------|---------|
| `run.py` | **Daily driver** — single command for non-technical users |
| `setup_wizard.py` | First-time setup (called automatically by `run.py`) |
| `transcribe_batch.py` | MP4 → VTT via faster-whisper; resume-safe, per-file timeout |
| `analyze_copilot.py` | Analysis engine — both BU-flat and UC/feature pipelines |

Scripts in `archive/` are retired (`analyze.py`, `analyze_uc.py`, `whisper_batch.ps1`) — they require a GitHub PAT and `openai-whisper`, neither available on the Siemens environment.

## Setup

```bash
pip install -r requirements.txt
# edit .env: set TRANSCRIPTS_PATH (and optionally OUTPUT_PATH)
# or just run: python run.py  — setup wizard runs on first launch
```

Gitignored directories that must be populated manually:
- `client_context/` — `program_brief.txt`, `rolodex.txt`, `solution_prompt.txt`
- `client_data/` — the three backlog CSVs (UC0, UC1, UC2)

## Architecture

**`run.py`** orchestrates everything for a non-technical user:
1. Detects untranscribed MP4s, reports count + estimated time, offers to transcribe
2. Scans for new/changed transcripts
3. Runs `analyze_copilot.py --uc --transcript-only` (per-transcript analysis)
4. Runs `analyze_copilot.py --uc --summary-only` (feature + UC rollups)
5. Regenerates gap report
6. Generates changelog and context review suggestions

**`analyze_copilot.py`** has two modes:

*UC pipeline* (`--uc` flag):
1. `load_feature_registry()` — parses three CSVs into `Feature` objects keyed by numeric prefix
2. `match_folders_to_features()` — matches `TRANSCRIPTS_PATH` subdirs to features by prefix
3. `uc_analyze_transcript()` — VTT/TXT → clean text, feature context injected, Copilot CLI call
4. `uc_summarize_feature()` — per-feature rollup across all transcript analyses
5. `uc_summarize_uc()` — UC-level rollup (UC0/1/2), filtered by scope
6. `generate_gap_report()` — pure data, no API call; coverage matrix + priority gap list

*BU pipeline* (default, no `--uc`):
1. `discover_bus()` — groups transcripts by immediate subfolder name
2. `analyze_transcript()` — VTT/TXT parse + Copilot CLI call
3. `summarize_bu()` — BU-level rollup

**API transport:** Copilot CLI (`copilot -p ... -s`). Large payloads written to temp files to avoid Windows command-line length limits. Retry logic with exponential backoff for transient spawn failures.

**Resume safety:** Each transcript gets a `.meta.json` sidecar tracking source hash, size, mtime, model, and prompt signature. Re-runs only process transcripts where any of those changed.

**Rate limits:** Copilot Business — Haiku ~2000/day, Sonnet ~50/day. 190 transcripts = 1–2 days of analysis runs.

## Key Flags

```bash
python run.py                          # normal daily run
python run.py --gap-only               # regenerate gap report only (no API calls)
python run.py --qc-only                # transcript quality check only
python run.py --summary-only           # re-run summaries from existing analyses
python run.py --setup                  # re-run setup wizard

python analyze_copilot.py --uc --gap-only       # gap report, no API
python analyze_copilot.py --uc --feature 1.04   # single feature
python analyze_copilot.py --uc                  # full UC pipeline
python analyze_copilot.py --bu "ARM"            # single BU (BU pipeline)
```

## Known Constraints

- `get_transcripts.ps1` blocked by corporate Conditional Access — use `transcribe_batch.py` on MP4s instead
- `ffmpeg` required for transcription: `winget install Gyan.FFmpeg`
- Copilot CLI can hang — `COPILOT_TIMEOUT_SECONDS` (default 900) is the hard ceiling
- Whisper CPU speed: `small` model ~4x realtime; 190 x 1hr meetings ≈ 2 days unattended
