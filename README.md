# Client Transcript Analyzer

Bulk-analyzes Teams meeting transcripts using Claude (via GitHub Models API) and produces structured summaries keyed to a backlog of design features.

Two scripts, two modes:

| Script | Purpose |
|--------|---------|
| `analyze.py` | Original BU-flat pipeline — walks folders by business unit |
| `analyze_uc.py` | Use-case pipeline — maps transcripts to backlog features, produces gap report |

For new work, use `analyze_uc.py`.

---

## Prerequisites

- Python 3.9+
- A GitHub PAT with `models:read` scope → [github.com/settings/tokens](https://github.com/settings/tokens)
- Transcript files (`.vtt` or `.txt`) on disk

---

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Create config file**

```bash
mkdir -p ~/.config/client-transcript-analyzer
cp .env.example ~/.config/client-transcript-analyzer/.env
```

Edit the `.env`:

```
GITHUB_TOKEN=ghp_...          # GitHub PAT with models:read scope
TRANSCRIPTS_PATH=/path/to/transcripts
OUTPUT_PATH=./output          # optional, defaults to ./output
```

**3. Populate `client_context/` (gitignored — copy manually)**

| File | Contents |
|------|---------|
| `program_brief.txt` | `Program_Context_Brief.md` from Workcall Drive |
| `rolodex.txt` | `04_people_rolodex.md` from Workcall Drive |
| `solution_prompt.txt` | SOLUTION prompt block from `PromptLibrary.md` (optional — fallback prompt used if absent) |

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

Designed for transcripts organized into feature folders (e.g. `1.04 Catalogue and Pricebook/`). Matches folders to backlog features by numeric prefix, analyzes each transcript with the feature definition injected as context, and produces a gap report against the full backlog.

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

**Commands:**

```bash
# Check coverage before running any API calls
python analyze_uc.py --gap-only

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
    1.04 Catalogue and Pricebook/
      recording1 [ANALYZED].txt       ← per-transcript extraction
    [FEATURE SUMMARY] 1.04 Catalogue and Pricebook.md
  [UC SUMMARY] UC0.md                 ← synthesized across all in-scope features
  [UC SUMMARY] UC1.md
  [UC SUMMARY] UC2.md
  [GAP REPORT] coverage.md            ← which backlog rows have no transcripts
```

**Start here.** Run `--gap-only` first to see what you have before spending any API quota.

---

## analyze.py — BU-Flat Pipeline

Original pipeline. Walks `TRANSCRIPTS_PATH` for `.vtt` files grouped by immediate subfolder (= BU name).

```bash
python analyze.py                  # all BUs
python analyze.py --bu "ARM"       # single BU
python analyze.py --summary-only   # re-run summaries from existing [ANALYZED] files
```

**Outputs:**

```
output/
  <BU>/
    <filename> [ANALYZED].txt
    [BU SUMMARY] <BU>.md
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

**Option A — Whisper from MP4 (recommended)**

If you have the raw recordings locally:

```powershell
whisper "meeting.mp4" --model medium --output_format vtt --output_dir C:\Transcripts
```

Note: `ffmpeg` must be installed (`winget install Gyan.FFmpeg`). CPU speed is ~0.25x realtime with `medium`; use `--model small` for 4-5x speedup with acceptable accuracy loss.

**Option B — SharePoint VTT download**

`get_transcripts.ps1` is implemented but blocked by corporate Conditional Access. Requires IT to whitelist an app registration. See `HANDOVER.md` for details.
