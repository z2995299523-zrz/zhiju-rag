param([ValidateRange(1, 65535)][int]$Port = 8781, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$launchArgs = @((Join-Path $PSScriptRoot "launch.py"))
if ($PSBoundParameters.ContainsKey("Port")) { $launchArgs += @("--port", $Port) }
if ($NoBrowser) { $launchArgs += "--no-browser" }
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    & py.exe -3 @launchArgs
} else {
    & python.exe @launchArgs
}
exit $LASTEXITCODE
