# Measure startup time of spectrum exe: time until a top-level window titled "Spectrum" appears.
param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [int]$Runs = 3
)
$ErrorActionPreference = 'Stop'

# kill leftovers so a stale window can't be picked up
Get-Process spectrum -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

$times = @()
for ($i = 1; $i -le $Runs; $i++) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $Exe -PassThru
    $visible = $false
    while ($sw.Elapsed.TotalSeconds -lt 120) {
        # onefile: window belongs to the bootloader's child process, poll by name
        $hit = Get-Process spectrum -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowTitle -eq 'Spectrum' }
        if ($hit) { $visible = $true; break }
        Start-Sleep -Milliseconds 20
    }
    $sw.Stop()
    $times += [math]::Round($sw.Elapsed.TotalSeconds, 2)
    Write-Host ("run {0}: {1:N2}s (window={2})" -f $i, $sw.Elapsed.TotalSeconds, $visible)
    Get-Process spectrum -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 800
}
$avg = ($times | Measure-Object -Average).Average
Write-Host ("average: {0:N2}s  runs: {1}" -f $avg, ($times -join ', '))
