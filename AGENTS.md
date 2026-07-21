# Client Transcript Analyzer — AI Agent Guidance

## Project Overview

A two-phase client engagement analysis tool supporting the **Siemens SherpaX program** (Revenue Cloud implementation). Processes ~190 Teams meeting recordings organized by business unit (DI-SW, SI), produces actionable per-BU summaries of requirements, decisions, and open items.

- **Phase 1 (active):** Transcribe MP4s → analyze transcripts → generate BU summaries
- **Phase 2 (planned):** Feed BU summaries + Confluence docs → find design gaps

**Why it matters:** Reduces manual review burden on program stakeholders; surfaces cross-BU patterns and decision conflicts.

---

## Architecture & Key Files

| File | Purpose |
|------|---------|
| `analyze.py` | Main orchestrator: walks transcripts, calls Claude API, generates per-BU summaries |
| `get_transcripts.ps1` | PowerShell: bulk-downloads `.vtt` files from SharePoint (device-code auth; currently blocked by Conditional Access) |
| `requirements.txt` | Python deps: `openai`, `python-dotenv` |
| `client_context/` | Gitignored context files (program brief, rolodex, solution prompt) — copied manually to VM |
| `output/` | Generated per-BU summaries and per-transcript analyses |

---

## Setup & Configuration

### Environment

**Config location:** `~/.config/client-transcript-analyzer/.env`

```
GITHUB_TOKEN=ghp_...              # GitHub PAT (scope: models:read)
TRANSCRIPTS_PATH=/path/to/.vtt    # Directory with .vtt files (organized by BU subfolder)
OUTPUT_PATH=/path/to/output       # [optional] defaults to ./output
RATE_LIMIT_SLEEP=2                # [optional] seconds between API calls (stay under rate limits)
MODEL_TRANSCRIPT=claude-3-5-haiku-20241022      # [optional] Haiku for per-transcript (cheap, fast)
MODEL_SUMMARY=claude-3-5-sonnet-20241022        # [optional] Sonnet for summaries (more capable)
```

**Why Haiku + Sonnet?** Haiku handles high-volume transcript analysis (low cost, hit rate limit freely). Sonnet generates better cross-BU summaries; used sparingly (`--summary-only` mode).

### Context Files (Gitignored)

Place these in `client_context/` **before first run**. All optional; gracefully degrade without them.

- **`program_brief.txt`** — Program context (from Workcall Drive → `Program_Context_Brief.md`). Helps Claude understand the Revenue Cloud architecture, key decision frameworks, and business drivers.
- **`rolodex.txt`** — People reference (from `04_people_rolodex.md`). Critical for transcript analysis — normalizes ambiguous names, captures who speaks which language / expertise area.
- **`solution_prompt.txt`** — Analysis prompt template (from `PromptLibrary.md` → copy the fenced code block only, not the heading). Defines what to extract from transcripts.

---

## Usage

### Typical Workflow

```bash
# Phase 1a: Transcribe all MP4s (on Windows VM only)
# ✓ ffmpeg must be installed: winget install Gyan.FFmpeg
# ✓ See HANDOVER.md for batch PowerShell script
# Produces: ~190 .vtt files in C:\Transcripts organized by BU

# Phase 1b: Analyze all transcripts
python analyze.py
# Outputs per-BU summary files to ./output

# Re-run summaries only (without re-analyzing each transcript)
python analyze.py --summary-only

# Single BU (debug / iteration)
python analyze.py --bu "Finance"
```

### Command-Line Interface

```
python analyze.py                 # Analyze all .vtt files, regenerate per-BU summaries
python analyze.py --bu "Finance"  # Analyze only Finance BU
python analyze.py --summary-only  # Re-aggregate from existing per-transcript analyses (no API calls)
```

---

## Key Constraints & Patterns

### API Throttling
- GitHub Models inference has **rate limits** (varies by org).
- `RATE_LIMIT_SLEEP=2` (default) adds 2 seconds between calls — **do not reduce below 1.5 without testing**.
- If you hit rate limits, the script exits; resume with `--summary-only` once limits reset.

### Transcript Organization
- **Expected structure:**
  ```
  TRANSCRIPTS_PATH/
    Finance/
      *.vtt
    Operations/
      *.vtt
    Sales/
      *.vtt
  ```
- BU folders inferred from directory names; per-BU summaries output to `OUTPUT_PATH/Finance_summary.md`, etc.

### Model Selection Rationale
- **Haiku** (fast, cheap): 1–2 min per 1-hour transcript. Good for "extract decisions and attendees."
- **Sonnet** (slower, capable): ~5–10 min per 1-hour transcript. Better for "generate cross-BU themes."
- Use Haiku for Phase 1 (all 190 transcripts). Sonnet only for final summary synthesis or high-value calls.

---

## Common Workflows for Agents

### Add New Analysis Prompt
1. Update `client_context/solution_prompt.txt` with new extraction logic (e.g., "also flag cost impacts").
2. Run `python analyze.py --summary-only` to re-aggregate without re-calling the API.
3. If you need full re-analysis (different model/prompt), delete `output/` and re-run.

### Debug a Failing Transcript
1. Single-BU run: `python analyze.py --bu "Finance"`
2. Check `output/Finance_analysis.json` for partial/error states.
3. Adjust `RATE_LIMIT_SLEEP` if throttling, or adjust `solution_prompt.txt` if extraction logic fails.

### Switch Models for Cost Reduction
- Change `MODEL_TRANSCRIPT` in `.env` to a smaller model (e.g., `claude-3-haiku-20240307`).
- Re-run: `python analyze.py --summary-only` first (uses cached transcript analyses if no model change detected).

### Phase 2 Integration (Future)
- BU summaries (`output/*.md`) feed into a gap-analysis flow against Confluence docs.
- Summaries follow a consistent markdown structure (decisions, action items, unknowns).
- See [HANDOVER.md](HANDOVER.md) for current VM setup status.

---

## Debugging & Troubleshooting

| Symptom | Check |
|---------|-------|
| `FileNotFoundError: TRANSCRIPTS_PATH` | Verify `.env` path exists and is readable; check `TRANSCRIPTS_PATH` for `.vtt` files |
| Rate limit errors (429) | Increase `RATE_LIMIT_SLEEP` in `.env`; wait ~15 min before retry |
| Rolls over without progress | Check `OUTPUT_PATH` is writable; verify no `.vtt` files are locked |
| Low-quality summaries | Populate all three `client_context/` files (especially `rolodex.txt`) |

---

## For VS Code Developers

When editing `analyze.py`:
- **Preserve the config-driven design** — all paths and API settings should come from `.env`, not hardcoded.
- **Keep context loading optional** — script should work even if `client_context/` files are missing.
- **Respect rate limits** — do not reduce `RATE_LIMIT_SLEEP` defaults without explicit user override.
- **Model selection is intentional** — Haiku for volume, Sonnet for synthesis. Document any model swaps.

---

## References

- [HANDOVER.md](HANDOVER.md) — VM setup, ffmpeg installation, batch transcription script
- [GitHub Models Docs](https://github.com/marketplace/models) — API endpoints, model availability, rate limits
- Program context & decision history: Workcall Drive (`Program_Context_Brief.md`, `PromptLibrary.md`)
