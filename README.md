# Client Transcript Analyzer

Bulk-analyzes Teams meeting transcripts and produces structured summaries keyed to a backlog of design features.

## Quick start (new users)

```powershell
python run.py
```

That's it. On first launch, `run.py` detects that setup hasn't been done and walks you through everything interactively — dependencies, Copilot auth, folder configuration. On subsequent runs it scans for new transcripts, analyzes them, updates summaries, and writes a changelog showing what shifted since last time.

To redo setup at any time: `python run.py --setup`

For unattended/background use (e.g. large backfills launched as a detached process with no one watching the terminal), add `--yes` to auto-confirm the "analyze N transcripts?" prompts:

```powershell
python run.py --yes
```

Note: `--yes` only skips the confirmation prompts in the normal run flow. First-time setup (`--setup` / `setup_wizard.py`) is still interactive by design, since it collects your GitHub PAT and folder paths — run it once manually before automating `run.py`.

---

## Scripts overview

| Script | Purpose |
|--------|---------|
| `run.py` | **Daily driver** — guided setup on first run, then scan → transcribe (optional) → analyze → summaries → gap report → changelog |
| `setup_wizard.py` | First-time setup wizard (called automatically by `run.py`) |
| `transcribe_batch.py` | Batch-transcribe MP4 recordings to VTT using faster-whisper |
| `analyze_copilot.py` | Analysis engine — BU pipeline and UC/feature pipeline with gap report |

For most users: **only `run.py` is needed day-to-day.**

Scripts in `archive/` (`analyze.py`, `analyze_uc.py`, `whisper_batch.ps1`) are retired
— they require a GitHub PAT and the old `openai-whisper` package, neither of which work
on the Siemens Copilot-only environment.

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

## UC pipeline and gap report

`run.py` runs the UC pipeline automatically on every full run. You can also drive it directly:

```bash
# Check coverage before any API calls (fast — pure data)
python analyze_copilot.py --uc --gap-only

# Full UC pipeline: analyze all features, UC summaries, gap report
python analyze_copilot.py --uc

# Single feature
python analyze_copilot.py --uc --feature 1.04

# Re-run summaries from existing analyses
python analyze_copilot.py --uc --summary-only

# Quality check (no API calls)
python analyze_copilot.py --uc --qc-only
```

**Expected `TRANSCRIPTS_PATH` layout for UC pipeline:**

```
TRANSCRIPTS_PATH/
  1.04 Catalogue and Pricebook/
    recording1.vtt
    recording2.vtt
  1.05 Quote Cloning and Versioning/
    recording1.vtt
```

Folder names are matched by leading numeric prefix (`1.04`, `2.01`, etc.) — typos after the prefix don't matter.

**UC pipeline outputs:**

```
output/
  features/
    1.04/
      recording1 [ANALYZED].txt        ← per-transcript extraction
      recording1 [ANALYZED].meta.json  ← cache/freshness metadata
    [FEATURE SUMMARY] 1.04 Catalogue and Pricebook.md
  [UC SUMMARY] UC0.md
  [UC SUMMARY] UC1.md
  [UC SUMMARY] UC2.md
  [GAP REPORT] coverage.md             ← which backlog rows have no transcript coverage
  [QC REPORT] transcript_quality.md
  logs/
    run_<timestamp>.log
    errors_<timestamp>.log
```

## BU pipeline (direct use)

If you want BU-flat summaries without UC/feature alignment:

```bash
python analyze_copilot.py                  # all BUs
python analyze_copilot.py --bu "ARM"       # single BU
python analyze_copilot.py --transcript-only
python analyze_copilot.py --summary-only
python analyze_copilot.py --qc-only
```

Optional model overrides:

```text
COPILOT_MODEL_TRANSCRIPT=auto
COPILOT_MODEL_SUMMARY=auto
```

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
