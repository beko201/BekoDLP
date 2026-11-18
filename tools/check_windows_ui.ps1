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
    $candidate = Get-Process -Name 'BekoDLP-Windows-x64' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $data.executable -and $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -eq 'BekoDLP' } | Select-Object -First 1
    if ($candidate) {
        $element = [System.Windows.Automation.AutomationElement]::FromHandle($candidate.MainWindowHandle)
        if ($element.Current.Name -eq 'BekoDLP') {
            $applicationWindow = $element
            break
        }
    }
    Start-Sleep -Milliseconds 200
}
if (-not $applicationWindow) { throw 'The packaged application did not create a window.' }

function Find-Control([string]$Name) {
    $condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $Name)
    return $applicationWindow.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
}
function Invoke-Control([string]$Name) {
    $control = Find-Control $Name
    if (-not $control) { throw "Missing UI control: $Name" }
    $control.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}
function Set-ControlValue([string]$Name, [string]$Value) {
    $control = Find-Control $Name
    if (-not $control) { throw "Missing editable control: $Name" }
    $control.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($Value)
}

try {
    @{ name = $applicationWindow.Current.Name; type = $applicationWindow.Current.ControlType.ProgrammaticName; pid = $candidate.Id } |
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
    if ($data.dump_only) {
        $elements = $applicationWindow.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        $values = foreach ($element in $elements) {
            @{ name = $element.Current.Name; type = $element.Current.ControlType.ProgrammaticName }
        }
        $values | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $data.report -Encoding UTF8
    } else {
        if ($data.trim) {
            Invoke-Control $advancedLabel
            Set-ControlValue $trimStart '0.5'
            Set-ControlValue $trimEnd '2.5'
        }
        Set-ControlValue $urlLabel $data.url
        Invoke-Control $enqueueLabel
        Write-Output ('PASS: packaged ' + $data.language + ' UI input and download submission')
    }
} finally {
    if ($data.close_after) {
        [BekoWindowMessages]::PostMessage($candidate.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
    }
}
