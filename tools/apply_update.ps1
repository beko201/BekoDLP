param([Parameter(Mandatory = $true)][string]$Config, [switch]$NoDialog)
$ErrorActionPreference = 'Stop'
$stageFolder = $PSScriptRoot
$source = Join-Path $stageFolder 'update.exe'
$backup = Join-Path $stageFolder 'previous.exe'
$replaced = $false
function Get-Sha256([string]$FilePath) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($FilePath)
    try {
        return [System.BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}
try {
    $configPath = [System.IO.Path]::GetFullPath($Config)
    if ($configPath -ne (Join-Path $stageFolder 'update.json')) { throw 'Invalid update configuration path.' }
    $data = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $target = [System.IO.Path]::GetFullPath([string]$data.target)
    $targetFolder = [System.IO.Path]::GetDirectoryName($target)
    if ([System.IO.Path]::GetDirectoryName($stageFolder) -ne $targetFolder) { throw 'Invalid update target folder.' }
    if ([System.IO.Path]::GetExtension($target) -ne '.exe') { throw 'Invalid update target.' }
    $application = Get-Process -Id ([int]$data.pid) -ErrorAction SilentlyContinue
    if ($application) {
        if (-not $application.WaitForExit(180000)) { throw 'Application did not close; the old executable was preserved.' }
    }
    if ((Get-Sha256 $source) -ne $data.sha256) { throw 'Update checksum changed.' }
    if ((Get-Sha256 $target) -ne $data.original_sha256) { throw 'Installed executable changed.' }
    # File.Replace is atomic and keeps the old executable as a backup.
    $replaced = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            [System.IO.File]::Replace($source, $target, $backup)
            $replaced = $true
            break
        } catch [System.IO.IOException] {
            if ($attempt -eq 59) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $replaced) { throw 'The executable could not be replaced.' }
    try {
        Start-Process -FilePath $target -WorkingDirectory $targetFolder -ErrorAction Stop | Out-Null
    } catch {
        [System.IO.File]::Replace($backup, $target, $source)
        $replaced = $false
        throw
    }
    # Delete only files in this helper's own, validated staging folder.
    Remove-Item -LiteralPath $backup -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $configPath -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stageFolder -ErrorAction SilentlyContinue
} catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $stageFolder 'update-error.txt') -Encoding UTF8
    # Surface failure even after the original GUI has exited.
    if (-not $NoDialog) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show("Update failed. The previous executable is preserved or backed up in:`n$stageFolder", 'BekoDLP update') | Out-Null
    }
    exit 1
}
