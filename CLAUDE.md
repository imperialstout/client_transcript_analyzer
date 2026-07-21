# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

CLI tools for bulk-analyzing Teams meeting transcripts using Claude via the GitHub Models API. Two scripts:

- **`analyze_uc.py`** — primary script. Maps transcripts to a backlog of design features (from three CSVs in `client_data/`), analyzes each with feature context injected, produces per-feature summaries, per-UC rollups, and a gap report showing which backlog items have no transcript coverage.
- **`analyze.py`** — original BU-flat pipeline. Walks transcript folders by business unit name. Still works, not the focus of active development.

## Setup

See `README.md` for full setup. Short version:

```bash
pip install -r requirements.txt
cp .env.example ~/.config/client-transcript-analyzer/.env
# edit .env: set GITHUB_TOKEN and TRANSCRIPTS_PATH
```

Gitignored directories that must be populated manually:
- `client_context/` — `program_brief.txt`, `rolodex.txt`, `solution_prompt.txt`
- `client_data/` — the three backlog CSVs (UC0, UC1, UC2)

## Running analyze_uc.py

`TRANSCRIPTS_PATH` should contain immediate subdirs named by Siemens feature prefix (e.g. `1.04 Catalogue and Pricebook/`). Matching is by numeric prefix only — folder name variation is fine.

```bash
python analyze_uc.py --gap-only        # coverage matrix, no API calls — start here
python analyze_uc.py                   # full pipeline
python analyze_uc.py --feature 1.04   # single feature
python analyze_uc.py --summary-only   # re-run summaries from existing analyses
```

Outputs in `./output/features/<prefix>/`, plus `[UC SUMMARY] UC0/1/2.md` and `[GAP REPORT] coverage.md`.

## Architecture

**`analyze_uc.py`** pipeline:
1. `load_feature_registry()` — parses the three CSVs into a dict of `Feature` objects keyed by Siemens numeric prefix
2. `match_folders_to_features()` — walks `TRANSCRIPTS_PATH` subdirs, matches each to a feature by prefix
3. `analyze_transcript()` — converts `.vtt`/`.txt` to clean text, injects feature definition into system prompt, calls Haiku
4. `summarize_feature()` — bundles per-transcript analyses for a feature, calls Sonnet
5. `summarize_uc()` — bundles feature summaries for each UC (filtered by scope), calls Sonnet
6. `generate_gap_report()` — pure data, no API call; produces coverage matrix and priority gap list

**`analyze.py`** pipeline:
1. `discover_bus()` — finds `.vtt` files, groups by immediate parent folder
2. `analyze_transcript()` — same VTT parse + Haiku call
3. `summarize_bu()` — Sonnet call over all analyses for a BU

Both scripts are resume-safe — skip output files that already exist.

**API client:** `openai` SDK pointed at `https://models.inference.ai.azure.com` with a GitHub token.

**Rate limits:** Haiku ~2000/day, Sonnet ~50/day on Copilot Business. 190 transcripts = 1–2 days.

## Known State & Blockers

See `HANDOVER.md` for full detail. Short version:
- `get_transcripts.ps1` blocked by corporate Conditional Access — use Whisper on MP4s instead
- `ffmpeg` required for Whisper: `winget install Gyan.FFmpeg`
