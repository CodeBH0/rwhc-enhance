param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$projectFile = Join-Path $projectRoot "frontend\Rwhc.WinUI\Rwhc.WinUI.csproj"

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw "dotnet was not found. Install the .NET 10 SDK and try again."
}

$pythonPath = $env:RWHC_PYTHON
if (-not $pythonPath) {
    $projectPython = Join-Path $projectRoot ".venv314\Scripts\python.exe"
    if (Test-Path -LiteralPath $projectPython) {
        $pythonPath = $projectPython
    }
    else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            $pythonPath = $pythonCommand.Source
        }
    }
}
if (-not $pythonPath) {
    throw "Python was not found. Install Python and requirements.txt, or set RWHC_PYTHON."
}

$env:RWHC_PYTHON = $pythonPath
$env:RWHC_BACKEND_ROOT = $projectRoot
Push-Location $projectRoot
try {
    & $pythonPath -c "import backend_host"
    if ($LASTEXITCODE -ne 0) {
        throw "Python backend import failed. Install requirements.txt with the selected interpreter."
    }
    if ($CheckOnly) {
        Write-Output "WinUI launch check passed: dotnet and the Python backend are available."
        return
    }
    & dotnet run --project $projectFile -c $Configuration -p:Platform=x64
    if ($LASTEXITCODE -ne 0) {
        throw "WinUI build or launch failed. dotnet exit code: $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
