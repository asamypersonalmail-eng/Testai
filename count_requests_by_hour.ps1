<#
.SYNOPSIS
  Count log blocks by hour for a given block title (streams, safe for multi-GB files).
  PowerShell equivalent of count_requests_by_hour.py - no Python needed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\count_requests_by_hour.ps1 HDB_LOG09-25-2026.txt
  powershell -ExecutionPolicy Bypass -File .\count_requests_by_hour.ps1 HDB_LOG09-25-2026.txt -Csv hourly.csv
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][string[]]$Files,
    [string]$Title = "MDP  getCreditCards Auth Request",
    [string]$Csv
)

$timeRe = [regex]'Time is:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2})'
$lookahead = 3
$counts = New-Object 'System.Collections.Generic.SortedDictionary[string,long]'
$noTime = 0

foreach ($f in $Files) {
    $path = (Resolve-Path -LiteralPath $f).ProviderPath
    $reader = New-Object System.IO.StreamReader($path, [System.Text.Encoding]::UTF8, $true, 16MB)
    try {
        $pending = 0
        while ($null -ne ($line = $reader.ReadLine())) {
            if ($pending -gt 0) {
                $pending--
                $m = $timeRe.Match($line)
                if ($m.Success) {
                    $key = $m.Groups[1].Value + " " + $m.Groups[2].Value + ":00"
                    if ($counts.ContainsKey($key)) { $counts[$key]++ } else { $counts[$key] = 1 }
                    $pending = 0
                    continue
                }
                if ($pending -eq 0) { $noTime++ }
            }
            if ($line.StartsWith($Title, [System.StringComparison]::Ordinal) -and $line.TrimEnd() -ceq $Title) {
                if ($pending -gt 0) { $noTime++ }
                $pending = $lookahead
            }
        }
        if ($pending -gt 0) { $noTime++ }
    }
    finally {
        $reader.Close()
    }
}

$total = 0
Write-Output "Title: $Title"
Write-Output ("{0,-12}{1,-7}{2,10}" -f "Date", "Hour", "Count")
Write-Output ("-" * 29)
foreach ($k in $counts.Keys) {
    $d, $h = $k -split ' '
    Write-Output ("{0,-12}{1,-7}{2,10}" -f $d, $h, $counts[$k])
    $total += $counts[$k]
}
Write-Output ("-" * 29)
Write-Output ("{0,-19}{1,10}" -f "Total", $total)
if ($noTime -gt 0) { Write-Warning "$noTime title line(s) without a 'Time is:' line" }

if ($Csv) {
    $counts.GetEnumerator() | ForEach-Object {
        $d, $h = $_.Key -split ' '
        [pscustomobject]@{ date = $d; hour = $h; count = $_.Value }
    } | Export-Csv -LiteralPath $Csv -NoTypeInformation
    Write-Output "CSV written to $Csv"
}
