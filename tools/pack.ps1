[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$Version = (Get-Date -Format 'yyyyMMdd-HHmm')
)

# Windows delivery packer. Keep the exclusion and post-build verification rules
# in the same arrays: a package is valid only when both passes agree.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\pack.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\pack.ps1 a20
#   .\tools\pack.ps1 -Version a20
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$OutDir = Join-Path $Root 'dist'
$Out = Join-Path $OutDir "swimwear-imagegen-$Version.zip"

$ForbiddenDirs = @(
    '.secrets'
    '.git'
    '.idea'
    '.vscode'
    '__pycache__'
    '.pytest_cache'
    '.ruff_cache'
    '.vite-cache'
    '.import_linter_cache'
    '.tmp*'
    '*.egg-info'
    '.venv'
    'venv'
    'node_modules'
    'test-results'
    'playwright-report'
    'patch'
    'dist'
    'coverage'
)

# These directories may exist in an unpacked delivery, but their runtime data
# must never be shipped. This implementation does not emit empty directory rows.
$ContentOnlyDirs = @('storage')

$ForbiddenFiles = @(
    '.env'
    '*.env'
    '*.key'
    '*.pem'
    'comfyui/config.yaml'
    '*.pyc'
    '*.pyo'
    '*.tsbuildinfo'
    '.DS_Store'
)

$EnvExamples = @(
    '.env.example'
    'frontend/.env.example'
)

$Required = @(
    '.env.example'
    'frontend/.env.example'
    '.gitattributes'
    '.gitignore'
    '.github/workflows/ci.yml'
    'backend/tools/verify_delivery.py'
    'Makefile'
)

function ConvertTo-ArchivePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $relative = $Path.Substring($Root.Length).TrimStart('\', '/')
    return $relative.Replace('\', '/')
}

function Test-NameMatchesAny {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Patterns
    )

    foreach ($pattern in $Patterns) {
        if ($Name -like $pattern) {
            return $true
        }
    }
    return $false
}

function Test-AllowedEnvExample {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    return $EnvExamples -contains $ArchivePath
}

function Test-ForbiddenFile {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    if (Test-AllowedEnvExample -ArchivePath $ArchivePath) {
        return $false
    }

    $name = [System.IO.Path]::GetFileName($ArchivePath)
    if ($name -like '.env.*') {
        return $true
    }

    foreach ($pattern in $ForbiddenFiles) {
        if ($pattern.Contains('/')) {
            if (($ArchivePath -like $pattern) -or ($ArchivePath -like "*/$pattern")) {
                return $true
            }
        }
        elseif ($name -like $pattern) {
            return $true
        }
    }
    return $false
}

function Test-ForbiddenArchivePath {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    $parts = $ArchivePath.Split('/')
    if ($parts.Count -gt 1) {
        foreach ($part in $parts[0..($parts.Count - 2)]) {
            if (Test-NameMatchesAny -Name $part -Patterns $ForbiddenDirs) {
                return $true
            }
            if (Test-NameMatchesAny -Name $part -Patterns $ContentOnlyDirs) {
                return $true
            }
        }
    }
    return Test-ForbiddenFile -ArchivePath $ArchivePath
}

function Get-DeliveryFiles {
    $files = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
    $pending = [System.Collections.Generic.Stack[System.IO.DirectoryInfo]]::new()
    $pending.Push([System.IO.DirectoryInfo]::new($Root))

    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()

        foreach ($child in $directory.GetDirectories()) {
            if (Test-NameMatchesAny -Name $child.Name -Patterns $ForbiddenDirs) {
                continue
            }
            if (Test-NameMatchesAny -Name $child.Name -Patterns $ContentOnlyDirs) {
                continue
            }
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to follow directory reparse point: $(ConvertTo-ArchivePath $child.FullName)"
            }
            $pending.Push($child)
        }

        foreach ($file in $directory.GetFiles()) {
            $relative = ConvertTo-ArchivePath $file.FullName
            if (Test-ForbiddenFile -ArchivePath $relative) {
                continue
            }
            if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to package file reparse point: $relative"
            }
            $files.Add($file)
        }
    }

    return $files | Sort-Object { ConvertTo-ArchivePath $_.FullName }
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

Push-Location $Root
try {
    [System.IO.Directory]::CreateDirectory($OutDir) | Out-Null
    if ([System.IO.File]::Exists($Out)) {
        [System.IO.File]::Delete($Out)
    }

    $deliveryFiles = @(Get-DeliveryFiles)
    Write-Host "==> Packing $Out"

    $archive = [System.IO.Compression.ZipFile]::Open(
        $Out,
        [System.IO.Compression.ZipArchiveMode]::Create
    )
    try {
        foreach ($file in $deliveryFiles) {
            $relative = ConvertTo-ArchivePath $file.FullName
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive,
                $file.FullName,
                $relative,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    }
    finally {
        $archive.Dispose()
    }

    # Re-open the finished artifact and verify its actual contents. Never trust
    # the enumeration pass alone: a broken exclusion must fail closed.
    $listing = [System.Collections.Generic.List[string]]::new()
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Out)
    try {
        foreach ($entry in $archive.Entries) {
            $path = $entry.FullName
            while ($path.StartsWith('./', [System.StringComparison]::Ordinal)) {
                $path = $path.Substring(2)
            }
            if ($path) {
                $listing.Add($path)
            }
        }
    }
    finally {
        $archive.Dispose()
    }

    $failed = $false
    foreach ($path in $listing) {
        if (Test-ForbiddenArchivePath -ArchivePath $path) {
            Write-Error "Forbidden content found in delivery package: $path" -ErrorAction Continue
            $failed = $true
        }
    }
    foreach ($path in $Required) {
        if ($listing -notcontains $path) {
            Write-Error "Required file missing from delivery package: $path" -ErrorAction Continue
            $failed = $true
        }
    }

    if ($failed) {
        [System.IO.File]::Delete($Out)
        throw 'Package deleted because post-build verification failed.'
    }

    $size = [Math]::Round((Get-Item -LiteralPath $Out).Length / 1MB, 2)
    Write-Host "==> OK  $($listing.Count) files  ${size} MiB"
    Write-Host "    $Out"
}
catch {
    if ([System.IO.File]::Exists($Out)) {
        [System.IO.File]::Delete($Out)
    }
    Write-Error $_
    exit 1
}
finally {
    Pop-Location
}
