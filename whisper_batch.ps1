# whisper_batch.ps1 — batch-transcribe a folder of MP4 recordings using Whisper
#
# Usage:
#   .\whisper_batch.ps1 -Source "C:\path\to\recordings" -Output "C:\path\to\transcripts"
#
# Optional:
#   -Model      small | medium | large  (default: small — 4-5x faster, good for English)
#   -Pattern    file glob to match      (default: *.mp4)
#   -Recurse    also walk subfolders    (default: flat only)
#
# Resume-safe: skips files whose .vtt already exists in Output.
# Failures and skipped files are logged to Output\whisper_batch_log.txt.
#
# Requires: whisper (pip install openai-whisper), ffmpeg (winget install Gyan.FFmpeg)

param(
    [Parameter(Mandatory)][string]$Source,
    [Parameter(Mandatory)][string]$Output,
    [string]$Model   = "small",
    [string]$Pattern = "*.mp4",
    [switch]$Recurse
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

if (-not (Test-Path $Source)) {
    Write-Error "Source folder not found: $Source"
    exit 1
}

New-Item -ItemType Directory -Force -Path $Output | Out-Null

$logPath = Join-Path $Output "whisper_batch_log.txt"
function Log([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') | $msg"
    Write-Host $line
    Add-Content -Path $logPath -Value $line
}

# Quick dependency check
if (-not (Get-Command whisper -ErrorAction SilentlyContinue)) {
    Write-Error "whisper not found in PATH. Run: pip install openai-whisper"
    exit 1
}
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Error "ffprobe not found in PATH. Run: winget install Gyan.FFmpeg then restart shell."
    exit 1
}

# ---------------------------------------------------------------------------
# Discover files
# ---------------------------------------------------------------------------

$getParams = @{ Path = $Source; Filter = $Pattern }
if ($Recurse) { $getParams["Recurse"] = $true }

$files = Get-ChildItem @getParams | Where-Object { -not $_.PSIsContainer } | Sort-Object Name

$total   = $files.Count
$done    = 0
$skipped = 0
$failed  = 0

Log "=== whisper_batch start === source=$Source output=$Output model=$Model files=$total"

if ($total -eq 0) {
    Log "No files matching '$Pattern' found in $Source"
    exit 0
}

# ---------------------------------------------------------------------------
# Process each file
# ---------------------------------------------------------------------------

foreach ($file in $files) {
    $stem   = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
    $vttOut = Join-Path $Output "$stem.vtt"

    # Resume: skip if .vtt already exists
    if (Test-Path $vttOut) {
        Log "[skip] $($file.Name) — already transcribed"
        $skipped++
        continue
    }

    # Audio presence check — Teams sometimes creates video-only stubs
    $audioStreams = & ffprobe -v quiet -select_streams a -show_entries stream=codec_type `
        -of csv=p=0 $file.FullName 2>$null
    if (-not $audioStreams) {
        Log "[no-audio] $($file.Name) — no audio stream, skipping"
        $skipped++
        continue
    }

    Log "[transcribe] $($file.Name) ..."

    try {
        & whisper $file.FullName `
            --model $Model `
            --output_format vtt `
            --output_dir $Output `
            --language en

        if (Test-Path $vttOut) {
            $done++
            Log "[done] $($file.Name) -> $vttOut"
        } else {
            # Whisper exited 0 but no VTT — shouldn't happen but guard it
            throw "VTT file not found after whisper completed"
        }
    } catch {
        $failed++
        Log "[error] $($file.Name) — $_"
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Log "=== whisper_batch complete === done=$done skipped=$skipped failed=$failed"

if ($failed -gt 0) {
    Write-Host ""
    Write-Host "Some files failed. Check log: $logPath"
}
