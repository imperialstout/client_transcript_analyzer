# Handover: Client Transcript Analyzer — VM Setup Status

## What This Project Does

Processes 190 Teams meeting recordings organized by business unit, produces per-BU
summaries of requirements, decisions, and open items. Two-phase:

- **Phase 1 (in progress):** Transcribe MP4s → analyze transcripts → BU summaries
- **Phase 2 (future):** Feed BU summaries against Confluence docs to find design gaps

Repo: https://github.com/imperialstout/client_transcript_analyzer

---

## Current State

### What's Done
- `analyze.py` — fully written, ready to run once `.vtt` files exist
- `get_transcripts.ps1` — SharePoint VTT downloader (blocked by corporate Conditional Access, set aside)
- Python 3.11 installed on the VM via winget
- `openai-whisper` pip package installed

### What's Blocked
- `ffmpeg` is not installed — Whisper requires it to decode MP4 audio
- Once ffmpeg is installed, Whisper should work

---

## Next Step: Install ffmpeg

```powershell
winget install Gyan.FFmpeg
```

Close and reopen PowerShell after install, then verify:

```powershell
ffmpeg -version
```

Then test Whisper on one file (from the recordings folder):

```powershell
whisper "ARM Pricing HLD review-20260715_083238-Meeting Transcript.mp4" `
  --model medium `
  --output_format vtt `
  --output_dir C:\Transcripts
```

Expect ~10-15 minutes on CPU for a 1-hour recording. The `medium` model (~1.5GB)
was already downloaded on the first (failed) run so it won't re-download.

---

## Batch Transcription (all 190 files)

Once the single-file test works, run this to process everything:

```powershell
$recordings = "C:\Users\z004rr7z\OneDrive - Siemens AG\SX 3S CPQ - Recordings"
$output     = "C:\Transcripts"

Get-ChildItem $recordings -Recurse -Filter "*.mp4" | ForEach-Object {
    $vttName = [System.IO.Path]::ChangeExtension($_.Name, ".vtt")
    $subDir  = $_.DirectoryName.Replace($recordings, "").TrimStart("\")
    $outDir  = Join-Path $output $subDir

    if (Test-Path (Join-Path $outDir $vttName)) {
        Write-Host "[skip] $($_.Name)"
        return
    }

    Write-Host "[transcribe] $($_.Name)"
    whisper $_.FullName --model medium --output_format vtt --output_dir $outDir
}
```

Resume-safe — skips files already transcribed.

---

## Running the Analyzer (Phase 1)

Once `.vtt` files are in `C:\Transcripts`:

**1. Create config dir and .env:**
```powershell
mkdir "$env:USERPROFILE\.config\client-transcript-analyzer"
copy .env.example "$env:USERPROFILE\.config\client-transcript-analyzer\.env"
```

Edit the `.env`:
```
GITHUB_TOKEN=ghp_...        # GitHub PAT with models:read scope
TRANSCRIPTS_PATH=C:\Transcripts
```

**2. Populate client_context\ (gitignored — copy manually):**
- `client_context\program_brief.txt` — contents of `Program_Context_Brief.md` from Workcall Drive
- `client_context\rolodex.txt` — contents of `04_people_rolodex.md` from Workcall Drive
- `client_context\solution_prompt.txt` — SOLUTION prompt block from `PromptLibrary.md`

**3. Install Python deps:**
```powershell
python -m pip install -r requirements.txt
```

**4. Run:**
```powershell
# Test one BU first
python analyze.py --bu "ARM"

# Full run
python analyze.py
```

Outputs land in `C:\Transcripts\<BU>\`:
- `<filename> [ANALYZED].txt` per transcript
- `[BU SUMMARY] <BU>.md` per business unit

---

## GitHub Models API (LLM backend)

The analyzer calls Claude via GitHub Models — no Anthropic API key needed, uses a
GitHub PAT instead. Traffic goes to `models.inference.ai.azure.com` via GitHub auth.

Rate limits on Copilot Business:
- `claude-3-5-haiku` (individual transcripts): ~2,000 req/day
- `claude-3-5-sonnet` (BU summaries): ~50 req/day

At 190 transcripts, plan for 1-2 days of runs. The script is resume-safe.

To generate a GitHub PAT: github.com/settings/tokens → `models:read` scope.

---

## Abandoned Approaches (don't retry without IT help)

| Approach | Why it failed |
|---|---|
| PnP.PowerShell `-Interactive` | Corporate policy blocks browser popups from PowerShell |
| PnP.PowerShell `-DeviceLogin` | "Specified method not supported" — threading issue with PS7 |
| REST device-code flow | Error 53003 — Conditional Access blocks non-compliant auth flows |

The SharePoint VTT download path requires IT to either whitelist an app registration
or grant access to the transcript library. Not worth pursuing — Whisper from MP4 is
the cleaner path anyway since the files are already local.
