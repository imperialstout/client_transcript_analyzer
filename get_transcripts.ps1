# get_transcripts.ps1
#
# Bulk-downloads all .vtt transcript files from a SharePoint recordings library.
# Teams stores transcripts alongside MP4s but they're hidden from the default folder view.
#
# Prerequisites (run once, no admin rights needed):
#   Install-Module PnP.PowerShell -Scope CurrentUser -Force
#
# Usage:
#   .\get_transcripts.ps1 -SiteUrl "https://yourcompany.sharepoint.com/sites/yourteam" `
#                         -LibraryPath "Shared Documents/General/Recordings" `
#                         -OutputPath "C:\Transcripts"
#
# The script preserves the subfolder structure (BU folders) under OutputPath.
# Already-downloaded files are skipped on re-run.

param(
    [Parameter(Mandatory)]
    [string]$SiteUrl,

    # The server-relative path to the folder containing the recordings/BU subfolders.
    # Example: "Shared Documents/General/Recordings"
    [Parameter(Mandatory)]
    [string]$LibraryPath,

    # Local folder where .vtt files will be saved, preserving subfolder structure.
    [Parameter(Mandatory)]
    [string]$OutputPath
)

# ---------------------------------------------------------------------------
# Ensure PnP.PowerShell is available
# ---------------------------------------------------------------------------
if (-not (Get-Module -ListAvailable -Name PnP.PowerShell)) {
    Write-Host "PnP.PowerShell not found. Installing to CurrentUser scope..." -ForegroundColor Yellow
    Install-Module PnP.PowerShell -Scope CurrentUser -Force -AllowClobber
}

Import-Module PnP.PowerShell -ErrorAction Stop

# ---------------------------------------------------------------------------
# Connect (interactive browser login — uses your existing M365 session)
# ---------------------------------------------------------------------------
Write-Host "Connecting to $SiteUrl ..." -ForegroundColor Cyan
Connect-PnPOnline -Url $SiteUrl -DeviceLogin

# ---------------------------------------------------------------------------
# Recursively find all .vtt files under LibraryPath
# ---------------------------------------------------------------------------
Write-Host "Searching for .vtt files under '$LibraryPath' ..." -ForegroundColor Cyan

# Get all files recursively; PnP returns file objects with ServerRelativeUrl
$allFiles = Get-PnPFolderItem -FolderSiteRelativeUrl $LibraryPath -ItemType File -Recursive

$vttFiles = $allFiles | Where-Object { $_.Name -like "*.vtt" }

if ($vttFiles.Count -eq 0) {
    # Fallback: SharePoint search across the whole site for .vtt
    Write-Host "No .vtt files found via folder walk. Trying site-wide search..." -ForegroundColor Yellow
    $searchResults = Submit-PnPSearchQuery -Query "*.vtt" -SelectProperties "Title,Path,FileExtension" -All
    $vttFiles = $searchResults.ResultRows | Where-Object { $_["FileExtension"] -eq "vtt" }

    if ($vttFiles.Count -eq 0) {
        Write-Host "No .vtt files found. Verify that transcription was enabled for these meetings." -ForegroundColor Red
        exit 1
    }

    # For search results, download by URL rather than ServerRelativeUrl
    $useSearchResults = $true
} else {
    $useSearchResults = $false
}

Write-Host "Found $($vttFiles.Count) .vtt file(s)." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Download each .vtt, preserving folder structure
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Path $OutputPath -Force | Out-Null

$succeeded = 0
$skipped   = 0
$failed    = 0

foreach ($file in $vttFiles) {
    if ($useSearchResults) {
        $serverRelUrl = ([System.Uri]$file["Path"]).AbsolutePath
        $fileName     = Split-Path $serverRelUrl -Leaf
        $folderPart   = Split-Path $serverRelUrl -Parent
    } else {
        $serverRelUrl = $file.ServerRelativeUrl
        $fileName     = $file.Name
        $folderPart   = Split-Path $serverRelUrl -Parent
    }

    # Determine local subfolder by stripping the base library path
    $basePath   = "/" + $SiteUrl.Split("/")[3..99] -join "/" + "/" + $LibraryPath.TrimStart("/")
    $relative   = $folderPart -replace [regex]::Escape($basePath), ""
    $localDir   = Join-Path $OutputPath ($relative.TrimStart("/\").Replace("/", "\"))
    $localFile  = Join-Path $localDir $fileName

    if (Test-Path $localFile) {
        Write-Host "  [skip]     $fileName" -ForegroundColor DarkGray
        $skipped++
        continue
    }

    New-Item -ItemType Directory -Path $localDir -Force | Out-Null

    try {
        Get-PnPFile -Url $serverRelUrl -Path $localDir -FileName $fileName -AsFile -Force | Out-Null
        Write-Host "  [download] $fileName" -ForegroundColor Green
        $succeeded++
    } catch {
        Write-Host "  [error]    $fileName - $($_.Exception.Message)" -ForegroundColor Red
        $failed++
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Done. $succeeded downloaded, $skipped skipped, $failed failed." -ForegroundColor Cyan
Write-Host "Files saved to: $OutputPath"

Disconnect-PnPOnline
