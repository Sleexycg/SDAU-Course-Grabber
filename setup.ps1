[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$venvDir = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

function Get-PythonProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [string[]]$PrefixArgs = @()
    )

    try {
        $output = @(
            & $Command @PrefixArgs -c `
                "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        )
        $exitCode = $LASTEXITCODE
    }
    catch {
        return [PSCustomObject]@{
            CanStart   = $false
            IsSupported = $false
            Version    = $null
        }
    }

    $versionText = [string]($output | Select-Object -Last 1)
    if ($exitCode -ne 0 -or $versionText -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
        return [PSCustomObject]@{
            CanStart   = $false
            IsSupported = $false
            Version    = $null
        }
    }

    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    return [PSCustomObject]@{
        CanStart   = $true
        IsSupported = ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11))
        Version    = $versionText
    }
}

function Find-SystemPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExcludedPython
    )

    $candidates = @(
        [PSCustomObject]@{ Name = "python"; Args = @() },
        [PSCustomObject]@{ Name = "py"; Args = @("-3") },
        [PSCustomObject]@{ Name = "python3"; Args = @() }
    )
    $excludedFullPath = [System.IO.Path]::GetFullPath($ExcludedPython)

    foreach ($candidate in $candidates) {
        $commandInfo = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $commandInfo) {
            continue
        }

        $commandPath = [System.IO.Path]::GetFullPath($commandInfo.Source)
        if ([string]::Equals(
                $commandPath,
                $excludedFullPath,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            continue
        }

        $probe = Get-PythonProbe -Command $commandPath -PrefixArgs $candidate.Args
        if ($probe.CanStart -and $probe.IsSupported) {
            return [PSCustomObject]@{
                Command = $commandPath
                Args    = $candidate.Args
                Version = $probe.Version
            }
        }
    }
    return $null
}

function Assert-SafeVenvTarget {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$Target
    )

    $rootFullPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $targetFullPath = [System.IO.Path]::GetFullPath($Target).TrimEnd('\', '/')
    $expected = [System.IO.Path]::Combine($rootFullPath, ".venv")
    if (-not [string]::Equals(
            $targetFullPath,
            $expected,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            [System.IO.Path]::GetDirectoryName($targetFullPath),
            $rootFullPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Refusing to rebuild an unexpected virtual-environment path: $targetFullPath"
    }
    if (Test-Path -LiteralPath $targetFullPath) {
        $targetItem = Get-Item -LiteralPath $targetFullPath -Force
        if (($targetItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to rebuild .venv because it is a symbolic link or junction."
        }
    }
}

function Assert-LastCommand {
    param([Parameter(Mandatory = $true)][string]$Message)
    if ($LASTEXITCODE -ne 0) {
        throw $Message
    }
}

Push-Location $projectRoot
try {
    Write-Host "[1/4] Inspecting the project virtual environment ..." -ForegroundColor Cyan

    $reuseVenv = $false
    $venvProblem = "the virtual environment does not exist"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $venvProbe = Get-PythonProbe -Command $venvPython
        if ($venvProbe.CanStart -and $venvProbe.IsSupported) {
            $reuseVenv = $true
            Write-Host "      Reusing .venv (Python $($venvProbe.Version))" -ForegroundColor DarkGray
        }
        elseif ($venvProbe.CanStart) {
            $venvProblem = "its Python $($venvProbe.Version) is older than 3.11"
        }
        else {
            $venvProblem = "its Python executable cannot be started"
        }
    }
    elseif (Test-Path -LiteralPath $venvDir) {
        $venvProblem = "it is incomplete because Scripts\python.exe is missing"
    }

    if (-not $reuseVenv) {
        $bootstrap = Find-SystemPython -ExcludedPython $venvPython
        if (-not $bootstrap) {
            if (Test-Path -LiteralPath $venvDir) {
                throw (
                    "The existing .venv cannot be reused because $venvProblem, " +
                    "and no system Python 3.11 or newer was found. " +
                    "The existing .venv was not changed."
                )
            }
            throw (
                "No reusable .venv exists, and no system Python 3.11 or newer " +
                "was found. No files were removed."
            )
        }

        Assert-SafeVenvTarget -Root $projectRoot -Target $venvDir
        Write-Host (
            "[2/4] Rebuilding .venv with system Python $($bootstrap.Version) ..."
        ) -ForegroundColor Cyan
        if (Test-Path -LiteralPath $venvDir) {
            Remove-Item -LiteralPath $venvDir -Recurse -Force
        }
        $bootstrapArgs = @($bootstrap.Args)
        & $bootstrap.Command @bootstrapArgs -m venv $venvDir
        Assert-LastCommand "Failed to create the project .venv."

        $createdProbe = Get-PythonProbe -Command $venvPython
        if (-not ($createdProbe.CanStart -and $createdProbe.IsSupported)) {
            throw "The new .venv was created, but its Python executable is unusable."
        }
    }
    else {
        Write-Host "[2/4] No virtual-environment rebuild is required." -ForegroundColor Cyan
    }

    Write-Host "[3/4] Installing the project and PyInstaller ..." -ForegroundColor Cyan
    & $venvPython -m pip install -e ".[build]"
    Assert-LastCommand "Dependency installation failed. Check the network, proxy, and pip configuration."

    Write-Host "[4/4] Verifying the installation ..." -ForegroundColor Cyan
    & $venvPython -c "import main, PyInstaller; print('Python environment and PyInstaller are ready')"
    Assert-LastCommand "Installation verification failed."

    Write-Host ""
    Write-Host "Setup completed." -ForegroundColor Green
    Write-Host "Run:         .\.venv\Scripts\python.exe -m main"
    Write-Host "Build blank: .\.venv\Scripts\python.exe -m main build --blank"
}
catch {
    Write-Host ""
    Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
