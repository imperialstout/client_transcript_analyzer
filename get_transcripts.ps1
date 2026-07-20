# get_transcripts.ps1
#
# Bulk-downloads all .vtt transcript files from SharePoint using the
# Microsoft Graph device-code flow. No external modules required.
#
# Usage:
#   .\get_transcripts.ps1 -SiteUrl "https://yourcompany.sharepoint.com/sites/yoursite" `
#                         -OutputPath "C:\Transcripts"

param(
    [Parameter(Mandatory)]
    [string]$SiteUrl,

    [Parameter(Mandatory)]
    [string]$OutputPath
)

# ---------------------------------------------------------------------------
# Auth: device code flow — no modules, no browser popup
# Uses the well-known "Microsoft Office" first-party app which has
# SharePoint read access in every M365 tenant.
# ---------------------------------------------------------------------------
$clientId = "d3590ed6-52b3-4102-aeff-aad2292ab01c"   # Microsoft Office
$tenantId = "common"
$host_    = ([System.Uri]$SiteUrl).GetLeftPart([System.UriPartial]::Authority)
$scope    = "$host_/.default"

Write-Host "Requesting device code..." -ForegroundColor Cyan

$dcResp = Invoke-RestMethod -Method POST `
    -Uri "https://login.microsoftonline.com/$tenantId/oauth2/v2.0/devicecode" `
    -ContentType "application/x-www-form-urlencoded" `
    -Body "client_id=$clientId&scope=$([System.Uri]::EscapeDataString($scope))"

Write-Host ""
Write-Host $dcResp.message -ForegroundColor Yellow
Write-Host ""

# Poll until user completes login
$token    = $null
$interval = [int]$dcResp.interval
$expiry   = (Get-Date).AddSeconds([int]$dcResp.expires_in)

while (-not $token -and (Get-Date) -lt $expiry) {
    Start-Sleep -Seconds $interval
    try {
        $token = Invoke-RestMethod -Method POST `
            -Uri "https://login.microsoftonline.com/$tenantId/oauth2/v2.0/token" `
            -ContentType "application/x-www-form-urlencoded" `
            -Body ("client_id=$clientId" +
                   "&grant_type=urn:ietf:params:oauth:grant-type:device_code" +
                   "&device_code=$([System.Uri]::EscapeDataString($dcResp.device_code))")
    } catch {
        $body = $_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($body.error -eq "authorization_pending") { continue }
        if ($body.error -eq "slow_down")             { $interval += 5; continue }
        Write-Host "Auth error: $($body.error_description)" -ForegroundColor Red
        exit 1
    }
}

if (-not $token) { Write-Host "Timed out waiting for login." -ForegroundColor Red; exit 1 }

$headers = @{ Authorization = "Bearer $($token.access_token)" }
Write-Host "Authenticated." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Search for all .vtt files across the site
# ---------------------------------------------------------------------------
Write-Host "Searching for .vtt files..." -ForegroundColor Cyan

$searchUrl = "$host_/_api/search/query?" +
    "querytext='fileextension:vtt'&" +
    "selectproperties='Title,Path,FileExtension,ParentLink'&" +
    "rowlimit=500&" +
    "trimduplicates=false"

$searchResp = Invoke-RestMethod -Uri $searchUrl -Headers $headers -Method GET `
    -ContentType "application/json;odata=verbose" `
    -Headers ($headers + @{ Accept = "application/json;odata=verbose" })

$rows = $searchResp.d.query.PrimaryQueryResult.RelevantResults.Table.Rows.results

if (-not $rows -or $rows.Count -eq 0) {
    Write-Host "No .vtt files found via search. Trying direct folder walk of site root..." -ForegroundColor Yellow

    # Fallback: walk the default document library
    $listUrl = "$host_/_api/web/lists/getbytitle('Documents')/items?" +
               "`$filter=substringof('.vtt',FileLeafRef)&`$select=FileLeafRef,FileRef,FileDirRef"
    try {
        $listResp = Invoke-RestMethod -Uri $listUrl -Headers ($headers + @{ Accept = "application/json;odata=verbose" }) -Method GET
        $rows = $listResp.d.results | ForEach-Object {
            @{ Cells = @{ results = @(
                @{ Key = "Path"; Value = "$host_$($_.FileRef)" }
                @{ Key = "Title"; Value = $_.FileLeafRef }
            )}}
        }
    } catch {
        Write-Host "Fallback also failed: $_" -ForegroundColor Red
    }
}

if (-not $rows -or $rows.Count -eq 0) {
    Write-Host "No .vtt files found. The site may not have transcription enabled, or you may not have access to the transcript library." -ForegroundColor Red
    exit 1
}

Write-Host "Found $($rows.Count) .vtt file(s)." -ForegroundColor Green

# ---------------------------------------------------------------------------
# Download each file
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Path $OutputPath -Force | Out-Null

$succeeded = 0; $skipped = 0; $failed = 0

foreach ($row in $rows) {
    $cells    = $row.Cells.results
    $filePath = ($cells | Where-Object { $_.Key -eq "Path" }).Value
    $fileName = Split-Path $filePath -Leaf

    # Derive subfolder from the path (everything between site root and filename)
    $sitePath  = ([System.Uri]$SiteUrl).AbsolutePath.TrimEnd("/")
    $fileUri   = [System.Uri]$filePath
    $relPath   = $fileUri.AbsolutePath -replace "^$([regex]::Escape($sitePath))/", ""
    $subFolder = Split-Path $relPath -Parent
    $localDir  = Join-Path $OutputPath ($subFolder.Replace("/", "\"))
    $localFile = Join-Path $localDir $fileName

    if (Test-Path $localFile) {
        Write-Host "  [skip]     $fileName" -ForegroundColor DarkGray
        $skipped++
        continue
    }

    New-Item -ItemType Directory -Path $localDir -Force | Out-Null

    try {
        Invoke-RestMethod -Uri $filePath -Headers $headers -OutFile $localFile
        Write-Host "  [download] $fileName" -ForegroundColor Green
        $succeeded++
    } catch {
        Write-Host "  [error]    $fileName - $_" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host "Done. $succeeded downloaded, $skipped skipped, $failed failed." -ForegroundColor Cyan
Write-Host "Files saved to: $OutputPath"
