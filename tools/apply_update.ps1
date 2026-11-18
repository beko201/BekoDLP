param([Parameter(Mandatory = $true)][string]$Config, [switch]$NoDialog)
$ErrorActionPreference = 'Stop'
$stage = [IO.Path]::GetFullPath($PSScriptRoot)
$payload = Join-Path $stage 'payload'
$backup = Join-Path $stage 'rollback'
$managed = @('BekoDLP.exe', '_internal', 'tools', 'licenses', 'LICENSE', 'package-manifest.json')
$saved = [Collections.Generic.List[string]]::new()
$installed = [Collections.Generic.List[string]]::new()
$child = $null
$validated = $false
function LongPath([string]$Path) { return '\\?\' + [IO.Path]::GetFullPath($Path) }
function Hash([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead((LongPath $Path))
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}
function Exists([string]$Path) { return [IO.File]::Exists((LongPath $Path)) -or [IO.Directory]::Exists((LongPath $Path)) }
function AssertNoLinks([string]$Path) {
    if (([IO.File]::GetAttributes((LongPath $Path)) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Links are not allowed in managed application paths.' }
    if ([IO.Directory]::Exists((LongPath $Path))) {
        foreach ($item in [IO.Directory]::GetFileSystemEntries((LongPath $Path))) { AssertNoLinks $item.Substring(4) }
    }
}
function MoveEntry([string]$From, [string]$To) {
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            if ([IO.Directory]::Exists((LongPath $From))) { [IO.Directory]::Move((LongPath $From), (LongPath $To)) }
            else { [IO.File]::Move((LongPath $From), (LongPath $To)) }
            return
        } catch [IO.IOException] {
            if ($attempt -eq 59) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}
function DeleteOwned([string]$Path, [string]$Root) {
    $full = [IO.Path]::GetFullPath($Path)
    $scope = [IO.Path]::GetFullPath($Root)
    if ($full -ne $scope -and -not $full.StartsWith($scope + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup escaped its owned folder.' }
    if (Exists $full) {
        AssertNoLinks $full
        if ([IO.Directory]::Exists((LongPath $full))) { [IO.Directory]::Delete((LongPath $full), $true) }
        else { [IO.File]::Delete((LongPath $full)) }
    }
}
try {
    if ([IO.Path]::GetFullPath($Config) -ne (Join-Path $stage 'update.json')) { throw 'Invalid configuration path.' }
    $data = [IO.File]::ReadAllText($Config) | ConvertFrom-Json
    $target = [IO.Path]::GetFullPath([string]$data.target)
    $root = [IO.Path]::GetDirectoryName($target)
    if ([IO.Path]::GetFileName($target) -cne 'BekoDLP.exe' -or
        [IO.Path]::GetDirectoryName($stage) -ne [IO.Path]::GetDirectoryName($root) -or
        -not [IO.Path]::GetFileName($stage).StartsWith('.bekodlp-update-')) { throw 'Invalid update installation/staging folder.' }
    if ([string]$data.health_token -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid startup acknowledgement token.' }
    foreach ($path in @($stage, $root, [IO.Path]::GetDirectoryName($root))) {
        if (([IO.File]::GetAttributes((LongPath $path)) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Linked install/staging folders are not supported.' }
    }
    AssertNoLinks $payload
    foreach ($entry in $managed) { if (Exists (Join-Path $root $entry)) { AssertNoLinks (Join-Path $root $entry) } }
    if ((Hash (Join-Path $stage 'package.zip')) -ne $data.sha256) { throw 'Package checksum changed.' }
    $manifestPath = Join-Path $payload 'package-manifest.json'
    if ((Hash $manifestPath) -ne $data.manifest_sha256) { throw 'Manifest checksum changed.' }
    $manifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    if ($manifest.app -cne 'BekoDLP' -or $manifest.schema -ne 1 -or $manifest.layout -cne 'windows-x64-onedir' -or
        $manifest.health_protocol -ne 1 -or (@($manifest.managed_entries) -join '|') -cne ($managed -join '|')) { throw 'Unsupported package layout.' }
    $expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($row in $manifest.files) {
        $relative = [string]$row.path
        $parts = $relative.Split('/')
        if ($relative.Contains('\') -or $parts.Count -lt 1 -or $parts[0] -cnotin $managed -or
            $parts[0] -eq 'package-manifest.json' -or $relative -match '(^|/)\.\.?(/|$)|[<>:"|?*\x00-\x1f]') { throw 'Unsafe manifest path.' }
        $file = [IO.Path]::GetFullPath((Join-Path $payload $relative))
        if (-not $file.StartsWith($payload + '\', [StringComparison]::OrdinalIgnoreCase) -or -not $expected.Add($file)) { throw 'Duplicate or escaping manifest path.' }
        if (([IO.FileInfo]::new((LongPath $file))).Length -ne $row.size -or (Hash $file) -ne $row.sha256) { throw 'Payload checksum changed.' }
    }
    [void]$expected.Add($manifestPath)
    $actual = [IO.Directory]::GetFiles((LongPath $payload), '*', [IO.SearchOption]::AllDirectories)
    if ($actual.Count -ne $expected.Count) { throw 'Unexpected payload files.' }
    foreach ($file in $actual) { if (-not $expected.Contains($file.Substring(4))) { throw 'Unexpected payload path.' } }
    foreach ($entry in $managed) { if (-not (Exists (Join-Path $payload $entry))) { throw 'Incomplete application package.' } }
    $application = Get-Process -Id ([int]$data.pid) -ErrorAction SilentlyContinue
    if ($application -and -not $application.WaitForExit(180000)) { throw 'Application is still running; installation was postponed.' }
    foreach ($process in @(Get-Process -Name 'BekoDLP' -ErrorAction SilentlyContinue)) {
        if ($process.Path -eq $target) { throw 'Another instance is running. Close it before updating.' }
    }
    if ((Hash $target) -ne $data.original_sha256) { throw 'Installed executable changed.' }
    $validated = $true
    [IO.Directory]::CreateDirectory((LongPath $backup)) | Out-Null
    foreach ($entry in $managed) {
        if (Exists (Join-Path $root $entry)) {
            MoveEntry (Join-Path $root $entry) (Join-Path $backup $entry)
            $saved.Add($entry)
        }
    }
    foreach ($entry in @('_internal', 'tools', 'licenses', 'LICENSE', 'package-manifest.json', 'BekoDLP.exe')) {
        MoveEntry (Join-Path $payload $entry) (Join-Path $root $entry)
        $installed.Add($entry)
    }
    $marker = Join-Path $stage 'started.txt'
    if (Exists $marker) { throw 'Unexpected startup acknowledgement file.' }
    $env:BEKODLP_UPDATE_HEALTH_FILE = $marker
    $env:BEKODLP_UPDATE_HEALTH_TOKEN = $data.health_token
    # This is the interactive application, not the hidden updater helper.
    try { $child = Start-Process -FilePath $target -WorkingDirectory $root -WindowStyle Normal -PassThru -ErrorAction Stop }
    finally { Remove-Item Env:BEKODLP_UPDATE_HEALTH_FILE, Env:BEKODLP_UPDATE_HEALTH_TOKEN -ErrorAction SilentlyContinue }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        if ((Exists $marker) -and [IO.File]::ReadAllText($marker) -ceq $data.health_token) { break }
        if ($child.HasExited -or [DateTime]::UtcNow -gt $deadline) { throw 'The new application did not acknowledge startup.' }
        Start-Sleep -Milliseconds 100
    } while ($true)
    # A cleanup failure after successful startup must never roll back a running app.
    $validated = $false
    DeleteOwned $stage $stage
} catch {
    $failure = $_ | Out-String
    if ($validated) {
        try {
            if ($child -and -not $child.HasExited) { $child.Kill(); $child.WaitForExit(10000) | Out-Null }
            foreach ($entry in $installed) { DeleteOwned (Join-Path $root $entry) (Join-Path $root $entry) }
            foreach ($entry in $saved) {
                if ($entry -ne 'BekoDLP.exe') { MoveEntry (Join-Path $backup $entry) (Join-Path $root $entry) }
            }
            if ($saved.Contains('BekoDLP.exe')) { MoveEntry (Join-Path $backup 'BekoDLP.exe') $target }
        } catch { $failure += "`nRollback requires recovery from $backup : $($_ | Out-String)" }
    }
    $failure | Set-Content -LiteralPath (Join-Path $stage 'update-error.txt') -Encoding UTF8
    if (-not $NoDialog) {
        Add-Type -AssemblyName PresentationFramework
        [Windows.MessageBox]::Show("Update failed. Previous files were restored or retained in:`n$stage`nSee update-error.txt.", 'BekoDLP update') | Out-Null
    }
    exit 1
}
