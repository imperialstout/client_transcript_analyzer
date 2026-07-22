# Client Transcript Analyzer

Bulk-analyzes Teams meeting transcripts and produces structured summaries keyed to a backlog of design features.

Four scripts, two phases:

| Script | Purpose |
|--------|---------|
| `transcribe_batch.py` | **Phase 0** — batch-transcribe MP4 recordings to VTT using faster-whisper |
| `analyze_copilot.py` | **Phase 1** — BU-flat pipeline using GitHub Copilot CLI (primary active script) |
| `analyze.py` | Phase 1 alternative — same pipeline via direct GitHub Models API |
| `analyze_uc.py` | Phase 2 — maps transcripts to backlog features, produces gap report |

**Start with `transcribe_batch.py`** if you have raw MP4s. Then run `analyze_copilot.py` to produce summaries. Use `analyze_uc.py` when you want capability/use-case alignment against the backlog.

---

## Prerequisites

- Python 3.9+
- A GitHub PAT with `models:read` scope → [github.com/settings/tokens](https://github.com/settings/tokens)
- Transcript files (`.vtt` or `.txt`) on disk — or MP4 recordings (see `transcribe_batch.py`)
- `ffmpeg` in PATH for transcription: `winget install Gyan.FFmpeg`

---

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Create config file**

Windows (PowerShell):

```powershell
Copy-Item .env.example .env
```

macOS/Linux:

```bash
cp .env.example .env
```

Edit the `.env`:

```
GITHUB_TOKEN=ghp_...          # GitHub PAT with models:read scope
TRANSCRIPTS_PATH=/path/to/transcripts
OUTPUT_PATH=./output          # optional, defaults to ./output
```

The scripts now load `.env` from the project root first. The old `~/.config/client-transcript-analyzer/.env` location is only a fallback for legacy setups.

**3. Populate `client_context/` (gitignored — copy manually)**

| File | Contents |
|------|---------|
| `program_brief.txt` | `Program_Context_Brief.md` from Workcall Drive |
| `rolodex.txt` | `04_people_rolodex.md` from Workcall Drive |
| `solution_prompt.txt` | SOLUTION prompt block from `PromptLibrary.md` (optional). `analyze.py` uses this as the core transcript extraction prompt; `analyze_uc.py` includes it as additional global guidance if present. |

**4. Copy backlog CSVs into `client_data/`** (gitignored)

The three backlog CSVs belong in `client_data/`. Their filenames must match exactly:

```
client_data/
  20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - Epic UC0.csv
  20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - Epic UC 1.csv
  20260706_BACKLOGMASTER_INTERNAL_VER 2.0 - EpicUC02 Technician Quote.csv
```

---

## analyze_uc.py — Use-Case Pipeline

Designed for transcripts organized under immediate subfolders of `TRANSCRIPTS_PATH` (for example `1.04 Catalogue and Pricebook/`). Folders with numeric prefixes are matched to backlog features, while unmatched folders are still analyzed and reported as unmatched in the gap report.

Files directly under `TRANSCRIPTS_PATH` are also analyzed under a synthetic `ROOT` bucket.

**Expected `TRANSCRIPTS_PATH` layout:**

```
TRANSCRIPTS_PATH/
  1.04 Catalogue and Pricebook/
    recording1.vtt
    recording2.vtt
  1.05 Quote Cloning and Versioning/
    recording1.vtt
  1.09 Approval Workflow/
    ...
```

Folder names are matched by the leading numeric prefix (`1.04`, `2.01`, etc.) — exact name or typos after the prefix don't matter.

Accepted transcript formats are `.vtt` and `.txt`.

**Commands:**

```bash
# Check coverage before running any API calls
python analyze_uc.py --gap-only

# Quality-check transcript text before running API calls
python analyze_uc.py --qc-only

# Block low-quality transcripts from analysis (example threshold: 70)
python analyze_uc.py --qc-threshold 70

# Full pipeline: analyze all features, generate UC summaries + gap report
python analyze_uc.py

# Single feature (by numeric prefix)
python analyze_uc.py --feature 1.04

# Re-run summaries from already-analyzed transcripts (no per-transcript API calls)
python analyze_uc.py --summary-only
```

**Outputs:**

```
output/
  features/
    1.04/
      recording1 [ANALYZED].txt       ← per-transcript extraction
      recording1 [ANALYZED].meta.json ← cache/status metadata
    [FEATURE SUMMARY] 1.04 Catalogue and Pricebook.md
  [UC SUMMARY] UC0.md                 ← synthesized across all in-scope features
  [UC SUMMARY] UC1.md
  [UC SUMMARY] UC2.md
  [GAP REPORT] coverage.md            ← which backlog rows have no transcripts
  [QC REPORT] transcript_quality.md   ← transcript quality heuristics (no API calls)
  logs/
    run_<timestamp>.log               ← full console output for the run
    errors_<timestamp>.log            ← stderr/errors only
```

**Start here.** Run `--gap-only` first to see what you have before spending any API quota.

### How "already processed" is determined

`analyze_uc.py` now uses metadata sidecars (`[ANALYZED].meta.json`) for each transcript.

A transcript is skipped only when all of the following still match:

- source file hash (SHA-256)
- source file size and modification timestamp
- transcript model (`MODEL_TRANSCRIPT`)
- prompt/context signature (includes base context + `solution_prompt.txt` guidance)

If any of those change, the transcript is automatically reprocessed.

### Transcript quality pre-check (`--qc-only`)

`analyze_uc.py --qc-only` scans the same transcript set the pipeline would process (matched feature folders, unmatched folders, and root-level `.vtt`/`.txt` files) and writes:

- `output/[QC REPORT] transcript_quality.md`

The report assigns a heuristic quality score (`good` / `watch` / `poor`) using signals like:

- very short parsed text
- low word count
- repeated transcript lines
- frequent `inaudible`/`unclear` markers

Use this to identify transcripts that should be re-transcribed or manually reviewed before spending model quota.

### Run logs

`analyze_uc.py` now writes two log files under `output/logs/` for each run:

- `run_<timestamp>.log` — full run output including progress, skips, summaries, and errors
- `errors_<timestamp>.log` — error stream only

The script prints both log paths at startup.

### Transcript quality gate (`--qc-threshold`)

Use `--qc-threshold <0-100>` to prevent low-quality transcripts from being sent to the model at all.

Example:

```bash
python analyze_uc.py --qc-threshold 70
```

Behavior:

- `0` disables gating
- transcripts with QC score below the threshold are skipped before any API call
- skip decisions are recorded in `[ANALYZED].meta.json` with status `qc_blocked`
- if the transcript file changes later, it is automatically reconsidered on the next run

You can also set a default via env:

```text
QC_THRESHOLD=70
```

---

## analyze.py — BU-Flat Pipeline

Transcript-first pipeline. Walks `TRANSCRIPTS_PATH` for `.vtt` and `.txt` files grouped by immediate subfolder (= BU name), writes one `[ANALYZED].txt` per transcript, and can optionally generate BU summaries.

```bash
python analyze.py                  # all BUs
python analyze.py --bu "ARM"       # single BU
python analyze.py --transcript-only # per-transcript analyses only, no BU rollups
python analyze.py --qc-only         # transcript quality pre-check, no API calls
python analyze.py --qc-threshold 70 # skip transcripts below QC score 70 during analysis
python analyze.py --summary-only   # re-run summaries from existing [ANALYZED] files
```

**Outputs:**

```
output/
  <BU>/
    <filename> [ANALYZED].txt
    <filename> [ANALYZED].meta.json
    [BU SUMMARY] <BU>.md
  [QC REPORT] transcript_quality.md
  logs/
    run_<timestamp>.log
    errors_<timestamp>.log
```

If your immediate goal is to get a strong summary for each transcript and defer backlog/use-case alignment until later, this is the simpler path.

`analyze.py` now uses the same operational safeguards as the UC pipeline:

- `.vtt` and `.txt` inputs are both supported
- each transcript gets metadata sidecar caching via `[ANALYZED].meta.json`
- reprocessing happens automatically when the source file, prompt context, or model changes
- `--qc-only` generates a heuristic transcript quality report before any API calls
- `--qc-threshold` can block low-quality transcripts from analysis
- each run writes full and error-only logs under `output/logs/`

---

## analyze_copilot.py — BU-Flat Pipeline via Copilot CLI

`analyze_copilot.py` keeps the same BU pipeline behavior as `analyze.py`, but replaces direct model API calls with `copilot -p ... -s` requests. This avoids PAT/API plumbing and works with your signed-in Copilot session.

```bash
python analyze_copilot.py
python analyze_copilot.py --bu "ARM"
python analyze_copilot.py --transcript-only
python analyze_copilot.py --qc-only
python analyze_copilot.py --summary-only
```

Optional model overrides for Copilot CLI:

```text
COPILOT_MODEL_TRANSCRIPT=auto
COPILOT_MODEL_SUMMARY=auto
```

Notes:

- Large transcript/summary payloads are sent as temporary attachments to avoid command-length limits.
- Run `copilot --version` once in the same shell to verify the CLI is available.

---

## Rate Limits

Both scripts use Claude via GitHub Models (`models.inference.ai.azure.com`). Limits on Copilot Business:

| Model | Used for | Limit |
|-------|---------|-------|
| `claude-3-5-haiku` | Per-transcript analysis | ~2,000 req/day |
| `claude-3-5-sonnet` | Summaries | ~50 req/day |

Both scripts are resume-safe — skips output files that already exist. Safe to interrupt and rerun.

Override models via env vars:
```
MODEL_TRANSCRIPT=claude-3-5-haiku-20241022
MODEL_SUMMARY=claude-3-5-sonnet-20241022
RATE_LIMIT_SLEEP=2
```

---

## Getting Transcripts

**Option A — `transcribe_batch.py` (recommended)**

Batch-transcribes a folder of MP4 recordings to VTT using `faster-whisper`. Resume-safe, per-file timeout, failures logged to `NEEDS_FOLLOWUP.txt`.

```powershell
# Install once
pip install faster-whisper
winget install Gyan.FFmpeg   # restart shell after

# Test on one folder
python transcribe_batch.py --source "C:\Recordings\unsorted" --output "C:\Transcripts\unsorted"

# With model and timeout override
python transcribe_batch.py --source "C:\Recordings" --output "C:\Transcripts" --model medium --timeout 10800

# Walk subfolders
python transcribe_batch.py --source "C:\Recordings" --output "C:\Transcripts" --recurse
```

**Models:**

| Model | Speed (CPU) | Notes |
|-------|-------------|-------|
| `tiny` | ~8x realtime | Very fast; accuracy drops noticeably |
| `small` | ~4x realtime | **Default.** Good balance for English meeting audio |
| `medium` | ~1x realtime | Better accuracy; slower |
| `large-v2` | ~0.5x realtime | Best accuracy; plan for overnight runs |

`faster-whisper` uses `int8` quantization and VAD filtering (skips silence) — significantly faster than `openai-whisper` on the same hardware.

Note: transcripts produced by Whisper have no speaker attribution. The downstream analyzer extracts requirements and decisions from content alone, which is sufficient for discovery/design work.

**Option B — SharePoint VTT download**

`get_transcripts.ps1` is implemented but blocked by corporate Conditional Access. Requires IT to whitelist an app registration. See `HANDOVER.md` for details.
