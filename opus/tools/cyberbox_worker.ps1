# Isolated extract + Defender scan inside Windows Sandbox.
# Inbox is read-only from the host. Work happens on sandbox disk only.
$ErrorActionPreference = "Continue"
$inbox = "C:\Inbox"
$outbox = "C:\Outbox"
$work = "C:\CyberWork"
New-Item -ItemType Directory -Force -Path $work, $outbox | Out-Null

function Write-Report {
    param($payload)
    $json = $payload | ConvertTo-Json -Compress -Depth 6
    $path = Join-Path $outbox "report.json"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($path, $json, $utf8)
}

function Test-SafeName([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { return $false }
    if ($name.Contains("..")) { return $false }
    if ($name.StartsWith("\") -or $name.StartsWith("/")) { return $false }
    return $true
}

function Test-ZipSafety([string]$path) {
    $maxFiles = 4000
    $maxUncompressed = 512MB
    $minRatioSize = 50MB
    $maxRatio = 100
    $maxNested = 20
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        $zip = [System.IO.Compression.ZipFile]::OpenRead($path)
    } catch {
        return @{ blocked = $false }
    }
    try {
        $count = 0
        $uncomp = [int64]0
        $comp = [int64]0
        $nested = 0
        $slip = $false
        foreach ($entry in $zip.Entries) {
            $name = [string]$entry.FullName
            if ([string]::IsNullOrWhiteSpace($name) -or $name.EndsWith("/") -or $name.EndsWith("\")) { continue }
            $count++
            $uncomp += [int64]$entry.Length
            $comp += [int64]$entry.CompressedLength
            $norm = $name.Replace("\", "/")
            if ($norm.Contains("..") -or $norm.StartsWith("/") -or $norm.Contains(":")) { $slip = $true }
            if ($norm.ToLower().EndsWith(".zip") -or $norm.ToLower().EndsWith(".nupkg")) { $nested++ }
            if ($count -gt $maxFiles) {
                return @{ blocked = $true; reason = "zip_bomb_files"; zip_files = $count; uncompressed = $uncomp }
            }
        }
        if ($slip) {
            return @{ blocked = $true; reason = "zip_slip"; zip_files = $count; uncompressed = $uncomp }
        }
        if ($uncomp -gt $maxUncompressed) {
            return @{ blocked = $true; reason = "zip_bomb_size"; zip_files = $count; uncompressed = $uncomp }
        }
        if ($comp -gt 0 -and $uncomp -ge $minRatioSize -and ($uncomp / $comp) -ge $maxRatio) {
            return @{ blocked = $true; reason = "zip_bomb_ratio"; zip_files = $count; uncompressed = $uncomp; ratio = [int]($uncomp / $comp) }
        }
        if ($nested -ge $maxNested) {
            return @{ blocked = $true; reason = "nested_zip_bomb"; zip_files = $count; nested_zips = $nested }
        }
        return @{ blocked = $false }
    } finally {
        if ($zip) { $zip.Dispose() }
    }
}

function Expand-Archives([string]$root, [int]$rounds) {
    $extracted = 0
    $block = $null
    for ($i = 0; $i -lt $rounds; $i++) {
        $archives = Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -match '^\.(zip|nupkg|cab)$' }
        if (-not $archives) { break }
        foreach ($item in $archives) {
            $dest = Join-Path $item.DirectoryName ($item.BaseName + "_extracted")
            if (Test-Path -LiteralPath $dest) { continue }
            if ($item.Extension -match '^\.(zip|nupkg)$') {
                $check = Test-ZipSafety $item.FullName
                if ($check.blocked) {
                    $block = $check
                    return @{ extracted = $extracted; block = $block }
                }
            }
            try {
                New-Item -ItemType Directory -Force -Path $dest | Out-Null
                Expand-Archive -LiteralPath $item.FullName -DestinationPath $dest -Force -ErrorAction Stop | Out-Null
                $extracted++
            } catch {
                try {
                    tar -xf $item.FullName -C $dest 2>$null | Out-Null
                    $extracted++
                } catch {}
            }
        }
        $tars = Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '\.(tar|tgz|tar\.gz)$' }
        foreach ($item in $tars) {
            $dest = Join-Path $item.DirectoryName ($item.BaseName + "_extracted")
            if (Test-Path -LiteralPath $dest) { continue }
            try {
                New-Item -ItemType Directory -Force -Path $dest | Out-Null
                tar -xf $item.FullName -C $dest 2>$null | Out-Null
                $extracted++
            } catch {}
        }
    }
    return @{ extracted = $extracted; block = $block }
}

try {
    $sample = Join-Path $inbox "sample"
    if (Test-Path -LiteralPath $sample) {
        Copy-Item -LiteralPath $sample -Destination (Join-Path $work "sample") -Recurse -Force
    } else {
        Get-ChildItem -LiteralPath $inbox -Force | Where-Object { $_.Name -ne "run_checks.ps1" } | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $work -Recurse -Force
        }
    }

    $expand = Expand-Archives -root $work -rounds 3
    $archiveCount = [int]$expand.extracted
    $block = $expand.block
    $files = @(Get-ChildItem -LiteralPath $work -Recurse -File -ErrorAction SilentlyContinue)
    $risky = @(".exe", ".dll", ".scr", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".jar", ".com", ".hta", ".wsf", ".reg", ".lnk", ".iso")
    $riskyFiles = @($files | Where-Object { $risky -contains $_.Extension.ToLower() } | Select-Object -First 40 -ExpandProperty Name)

    $scanLog = Join-Path $outbox "defender.txt"
    $scanCode = -1
    $deadline = (Get-Date).AddSeconds(40)
    $mp = $null
    do {
        Start-Service WinDefend -ErrorAction SilentlyContinue
        Start-Service Sense -ErrorAction SilentlyContinue
        $mp = @(
            "C:\Program Files\Windows Defender\MpCmdRun.exe",
            "C:\Program Files (x86)\Windows Defender\MpCmdRun.exe"
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $mp) {
            $plat = Get-ChildItem "C:\ProgramData\Microsoft\Windows Defender\Platform" -Filter MpCmdRun.exe -Recurse -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($plat) { $mp = $plat.FullName }
        }
        if (-not $mp) {
            $cmd = Get-Command MpCmdRun.exe -ErrorAction SilentlyContinue
            if ($cmd) { $mp = $cmd.Source }
        }
        if ($mp) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    Get-Service *defend*,*sense* -ErrorAction SilentlyContinue | Format-Table Name, Status -AutoSize | Out-String | Out-File -FilePath $scanLog -Encoding utf8
    Get-ChildItem "C:\Program Files\Windows Defender" -ErrorAction SilentlyContinue | Out-String | Add-Content -LiteralPath $scanLog
    if ($mp) {
        & $mp -Scan -ScanType 3 -File $work | Out-File -FilePath $scanLog -Append -Encoding utf8
        $scanCode = $LASTEXITCODE
        Add-Content -LiteralPath $scanLog -Value ("mp=" + $mp + " code=" + $scanCode)
    } else {
        try {
            Start-MpScan -ScanType CustomScan -ScanPath $work -ErrorAction Stop
            $scanCode = 0
            Add-Content -LiteralPath $scanLog -Value "Start-MpScan completed."
        } catch {
            Add-Content -LiteralPath $scanLog -Value ("No Defender CLI inside the cyber box. " + $_.Exception.Message)
        }
    }

    $defenderLog = ""
    if (Test-Path -LiteralPath $scanLog) {
        $defenderLog = (Get-Content -LiteralPath $scanLog -Raw -ErrorAction SilentlyContinue)
        if ($defenderLog.Length -gt 8000) { $defenderLog = $defenderLog.Substring(0, 8000) }
    }

    $report = @{
        ok = $true
        isolated = $true
        threat = ($scanCode -eq 2)
        clean = (($scanCode -eq 0) -and -not $block)
        code = $scanCode
        files = $files.Count
        extracted_archives = $archiveCount
        risky = $riskyFiles
        defender_log = $defenderLog
        note = "Extract and Defender ran inside Windows Sandbox with networking off."
    }
    if ($block -and $block.blocked) {
        $report.blocked = $true
        $report.reason = [string]$block.reason
        $report.clean = $false
        $report.zip_files = $block.zip_files
        $report.uncompressed = $block.uncompressed
        $report.nested_zips = $block.nested_zips
        $report.ratio = $block.ratio
        $report.note = "Stopped extract inside the cyber box. Nothing from the sample was run."
    }
    Write-Report $report
} catch {
    Write-Report @{
        ok = $false
        isolated = $true
        threat = $false
        clean = $false
        code = -3
        error = [string]$_.Exception.Message
    }
}

Start-Process -FilePath shutdown.exe -ArgumentList "/s","/t","0" -WindowStyle Hidden
