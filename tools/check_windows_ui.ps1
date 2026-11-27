param([Parameter(Mandatory = $true)][string]$Config)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class BekoWindowMessages { [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam); }'
$data = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$deadline = [DateTime]::UtcNow.AddSeconds(60)
$applicationWindow = $null
while ([DateTime]::UtcNow -lt $deadline) {
    $candidate = Get-Process -Name 'BekoDLP' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $data.executable } | Select-Object -First 1
    if ($candidate) {
        # Qt can expose an auxiliary window as Process.MainWindowHandle after an update.
        $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($element in $windows) {
            if ($element.Current.ProcessId -eq $candidate.Id -and $element.Current.Name -eq 'BekoDLP') {
                $applicationWindow = $element
                break
            }
        }
        if ($applicationWindow) { break }
    }
    Start-Sleep -Milliseconds 200
}
if (-not $applicationWindow) { throw 'The packaged application did not create a window.' }
$applicationHandle = [IntPtr]$applicationWindow.Current.NativeWindowHandle

function Find-Control([string]$Name) {
    $condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $Name)
    $control = $applicationWindow.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($control) { return $control }
    $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($window in $windows) {
        if ($window.Current.ProcessId -eq $candidate.Id) {
            $control = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
            if ($control) { return $control }
        }
    }
    return $null
}
function Invoke-Control([string]$Name) {
    $control = Find-Control $Name
    if (-not $control) { throw "Missing UI control: $Name" }
    $control.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}
function Set-ControlValue([string]$Name, [string]$Value) {
    $control = Find-Control $Name
    if (-not $control) { throw "Missing editable control: $Name" }
    $rangePattern = $null
    if ($control.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$rangePattern)) {
        $rangePattern.SetValue([double]$Value)
    } else {
        $control.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($Value)
    }
}

try {
    @{ name = $applicationWindow.Current.Name; type = $applicationWindow.Current.ControlType.ProgrammaticName; pid = $candidate.Id; handle = $applicationHandle.ToInt64() } |
        ConvertTo-Json | Set-Content -LiteralPath ($data.report + '.window.json') -Encoding UTF8
    if ($applicationWindow.Current.Name -ne 'BekoDLP') { throw ('Unexpected window: ' + $applicationWindow.Current.Name) }
    if ($data.language -eq 'tr') {
        $urlLabel = 'Video bağlantısı'
        $enqueueLabel = 'Kuyruğa ekle'
        $advancedLabel = 'Gelişmiş seçenekler'
        $trimStart = 'Kırpma başlangıcı'
        $trimEnd = 'Kırpma bitişi'
    } else {
        $urlLabel = 'Video URL'
        $enqueueLabel = 'Add to queue'
        $advancedLabel = 'Advanced options'
        $trimStart = 'Trim start'
        $trimEnd = 'Trim end'
    }
    if ($data.inspect_restored_queue) {
        if (-not (Find-Control $data.expected_task_id)) { throw 'The saved task was not restored in the table.' }
        $resumeLabel = if ($data.language -eq 'tr') { 'Devam et' } else { 'Resume' }
        if (-not (Find-Control $resumeLabel)) { throw 'Restored pending tasks did not start paused.' }
        Write-Output ('PASS: packaged ' + $data.language + ' queue restored and paused')
    } elseif ($data.dump_only) {
        $elements = $applicationWindow.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        $values = foreach ($element in $elements) {
            @{ name = $element.Current.Name; type = $element.Current.ControlType.ProgrammaticName }
        }
        $values | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $data.report -Encoding UTF8
    } else {
        if ($data.trim -or $data.expect_cookie_file -or $data.expect_subtitles) {
            Invoke-Control $advancedLabel
        }
        if ($data.trim) {
            Set-ControlValue $trimStart $(if ($data.trim_start) { $data.trim_start } else { '0.5' })
            Set-ControlValue $trimEnd $(if ($data.trim_end) { $data.trim_end } else { '2.5' })
        }
        if ($data.expect_cookie_file) {
            $cookieName = if ($data.language -eq 'tr') { 'Çerez dosyası' } else { 'Cookie file' }
            $cookieControl = Find-Control $cookieName
            if (-not $cookieControl) { throw 'Cookie file control is missing.' }
            $actual = $cookieControl.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
            if ($actual -ne $data.cookie_file) { throw 'Cookie file preference was not restored in the UI.' }
        }
        if ($data.expect_subtitles) {
            $name = if ($data.language -eq 'tr') { 'Altyazı dilleri' } else { 'Subtitle languages' }
            $nameCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $name)
            $typeCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
            $condition = New-Object System.Windows.Automation.AndCondition($nameCondition, $typeCondition)
            $control = $applicationWindow.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
            if (-not $control) { throw 'Subtitle language control is missing.' }
            if ($control.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value -ne 'en') {
                throw 'Subtitle language preference was not restored in the UI.'
            }
        }
        Set-ControlValue $urlLabel $data.url
        if ($data.expect_playlist) {
            $bulkLabel = if ($data.language -eq 'tr') { 'Birden fazla bağlantı ekle' } else { 'Add multiple links' }
            $previewLabel = if ($data.language -eq 'tr') { 'Playlist videolarını seç…' } else { 'Choose playlist videos…' }
            $queueLabel = if ($data.language -eq 'tr') { 'Seçili videoları kuyruğa ekle' } else { 'Queue selected videos' }
            Invoke-Control $bulkLabel
            $bulkEditLabel = if ($data.language -eq 'tr') { 'Video veya playlist bağlantıları' } else { 'Video or playlist URLs' }
            $bulkDeadline = [DateTime]::UtcNow.AddSeconds(10)
            while (-not (Find-Control $bulkEditLabel) -and [DateTime]::UtcNow -lt $bulkDeadline) {
                Start-Sleep -Milliseconds 100
            }
            Set-ControlValue $bulkEditLabel $data.url
            Invoke-Control $previewLabel
            $playlistDeadline = [DateTime]::UtcNow.AddSeconds(35)
            $queueButton = $null
            while ([DateTime]::UtcNow -lt $playlistDeadline) {
                $queueButton = Find-Control $queueLabel
                if ($queueButton -and $queueButton.Current.IsEnabled) { break }
                Start-Sleep -Milliseconds 100
            }
            if (-not $queueButton -or -not $queueButton.Current.IsEnabled) { throw 'Playlist selection did not become ready.' }
            $firstLabel = if ($data.language -eq 'tr') { 'İlk playlist sırası' } else { 'First playlist position' }
            $lastLabel = if ($data.language -eq 'tr') { 'Son playlist sırası' } else { 'Last playlist position' }
            $rangeLabel = if ($data.language -eq 'tr') { 'Aralığı seç' } else { 'Select range' }
            $countLabel = if ($data.language -eq 'tr') { '3 videodan 1 tanesi seçili.' } else { '1 of 3 videos selected.' }
            Set-ControlValue $firstLabel '2'
            Set-ControlValue $lastLabel '2'
            Invoke-Control $rangeLabel
            $rangeDeadline = [DateTime]::UtcNow.AddSeconds(5)
            while (-not (Find-Control $countLabel) -and [DateTime]::UtcNow -lt $rangeDeadline) {
                Start-Sleep -Milliseconds 100
            }
            if (-not (Find-Control $countLabel)) { throw 'Playlist range did not select only position 2.' }
            $queueButton.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        } elseif ($data.expect_sources) {
            $sourceLabel = if ($data.language -eq 'tr') { 'Kaynak formatları…' } else { 'Source formats…' }
            $dialogLabel = if ($data.language -eq 'tr') { 'Kaynak formatları' } else { 'Source formats' }
            $queueLabel = if ($data.language -eq 'tr') { 'Seçili formatları kuyruğa ekle' } else { 'Queue selected formats' }
            Invoke-Control $sourceLabel
            $mainWindow = $applicationWindow
            $formatDeadline = [DateTime]::UtcNow.AddSeconds(35)
            $queueButton = $null
            while ([DateTime]::UtcNow -lt $formatDeadline) {
                $condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $dialogLabel)
                $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $condition)
                foreach ($window in $windows) {
                    if ($window.Current.ProcessId -eq $candidate.Id) { $applicationWindow = $window; break }
                }
                $queueButton = Find-Control $queueLabel
                if ($queueButton -and $queueButton.Current.IsEnabled) { break }
                Start-Sleep -Milliseconds 100
            }
            if (-not $queueButton -or -not $queueButton.Current.IsEnabled) { throw 'The format dialog did not expose a valid source choice.' }
            $queueButton.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            $applicationWindow = $mainWindow
        } else {
            Invoke-Control $enqueueLabel
        }
        Write-Output ('PASS: packaged ' + $data.language + ' UI input and download submission')
    }
} finally {
    if ($data.close_after) {
        [BekoWindowMessages]::PostMessage($applicationHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
    }
}
