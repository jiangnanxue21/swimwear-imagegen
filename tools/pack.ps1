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

# Ship the directory, but never the images inside it. `data/` is a scratch area
# on the developer machine: the previous package carried a 5.8 MB `data/s1.jpg`
# that nothing in the repository references, and no rule stopped it.
#
# The rule keys on the extension, not on the whole subtree, so a future
# `data/README.md` still ships. PowerShell's `-like` is case-insensitive, which
# is why this side needs no character-class expansion; the shell side does.
$ImageFreeDirs = @('data')

$ImageExtensions = @(
    'jpg'
    'jpeg'
    'png'
    'gif'
    'bmp'
    'webp'
    'tif'
    'tiff'
    'heic'
    'heif'
    'avif'
    'svg'
    'psd'
    'ico'
)

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
    # 运行期日志,含请求行、异常栈与图片绝对路径。与 pack.sh 的 FORBIDDEN_FILES
    # 逐条对齐。basename 匹配,任意层级都拦。
    '*.log'
    # Claude Code 个人权限配置,内含开发机绝对路径。与 pack.sh 的 FORBIDDEN_FILES
    # 逐条对齐 —— verify_delivery 盯着两侧不许分叉。basename 匹配,任意层级都拦。
    # 注:本数组内的注释不能出现 ASCII 右括号 —— verify_delivery 的解析正则
    # 非贪婪,遇到第一个右括号即截断,会把它后面的条目漏在数组之外。
    'settings.local.json'
)

# Kept in the repo, but deliberately not shipped. Unlike $ForbiddenFiles, a
# leak here is not an incident -- it is only wasted package size. Keeping the
# two lists apart keeps the word "forbidden" meaningful. Mirrors pack.sh's
# NOT_SHIPPED_FILES; verify_delivery compares the two arrays entry by entry.
# Note: no ASCII closing parenthesis inside these comments -- the verifier's
# regex is non-greedy and would truncate the array at the first one.
$NotShippedFiles = @(
    'docs/overview.html'
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
    'tools/normalize_eol.py'
    # The one implementation of the line-ending policy. It must travel with the
    # package: when the receiver repacks from the unpacked tree, the gate below
    # has to find it -- and "it was not in the package" is an avoidable
    # fail-closed.
)

function ConvertTo-ArchivePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $relative = $Path.Substring($Root.Length).TrimStart('\', '/')
    return $relative.Replace('\', '/')
}

function Find-RunnablePython {
    # `Get-Command` only proves that a command shim exists. Windows' App
    # Execution Alias leaves python.exe / python3.exe entries under WindowsApps
    # even when no interpreter is installed behind them; invoking that path then
    # fails with "system cannot find the path". That exact false positive used to
    # delete an otherwise valid package during the post-build EOL check.
    #
    # Prefer `python` so an activated virtualenv wins. Every candidate still has
    # to execute a tiny program before it is selected; a dead alias is skipped and
    # the launcher (`py`) remains a valid fallback on a normal Windows install.
    foreach ($candidate in @('python', 'py', 'python3')) {
        $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $command) { continue }

        try {
            & $command.Source -c 'import sys; raise SystemExit(0)' *> $null
            if ($LASTEXITCODE -eq 0) { return $command.Source }
        }
        catch {
            # A discovered but non-runnable WindowsApps alias is not Python.
            # Keep looking instead of treating discovery as successful.
        }
    }

    return $null
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

function Test-ImageUnderImageFreeDir {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    $parts = $ArchivePath.Split('/')
    if ($parts.Count -lt 2) {
        return $false
    }
    $extension = [System.IO.Path]::GetExtension($ArchivePath).TrimStart('.')
    if (-not $extension -or ($ImageExtensions -notcontains $extension.ToLowerInvariant())) {
        return $false
    }
    foreach ($part in $parts[0..($parts.Count - 2)]) {
        if ($ImageFreeDirs -contains $part) {
            return $true
        }
    }
    return $false
}

function Test-NotShippedFile {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    foreach ($pattern in $NotShippedFiles) {
        if (($ArchivePath -eq $pattern) -or ($ArchivePath -like "*/$pattern")) {
            return $true
        }
    }
    return $false
}

function Test-ForbiddenFile {
    param([Parameter(Mandatory = $true)][string]$ArchivePath)

    if (Test-AllowedEnvExample -ArchivePath $ArchivePath) {
        return $false
    }

    if (Test-ImageUnderImageFreeDir -ArchivePath $ArchivePath) {
        return $true
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
            if (Test-NotShippedFile -ArchivePath $relative) {
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

    # Parity with the Bash side (DECISIONS 3.69): the listing is durable
    # evidence for *every* run, not just failures -- the package may be
    # deleted or shipped away, this file stays. The Bash side additionally
    # needed the file to fix a SIGPIPE x pipefail false negative in its
    # required-file grep; this side compares in memory and never had that
    # failure mode, so the file here is evidence only.
    $listingFile = "$Out.listing.txt"
    [System.IO.File]::WriteAllLines($listingFile, $listing)

    $failed = $false
    foreach ($path in $listing) {
        if (Test-ForbiddenArchivePath -ArchivePath $path) {
            Write-Error "Forbidden content found in delivery package: $path" -ErrorAction Continue
            $failed = $true
        }
        # Verified after packing for the same reason as the forbidden list: an
        # exclusion rule that silently stopped matching produces a fat package
        # and no signal at all. Different wording on purpose -- this one is not
        # an incident, and an over-heavy warning teaches people to ignore it.
        elseif (Test-NotShippedFile -ArchivePath $path) {
            Write-Error "Not-shipped file found in delivery package: $path" -ErrorAction Continue
            Write-Host '   it belongs in the repo, just not in the package -- fix the exclusion list'
            $failed = $true
        }
    }
    # Same reasoning as the Bash side: "required file missing" has two very
    # different causes -- the file was genuinely excluded, or the archive listing
    # came back short -- and the fixes are opposite. Print what tells them apart.
    foreach ($path in $Required) {
        if ($listing -notcontains $path) {
            Write-Error "Required file missing from delivery package: $path" -ErrorAction Continue
            Write-Host "   diagnostic: listing had $($listing.Count) entries"
            Write-Host "   diagnostic: listing saved to $listingFile (the package gets deleted, this file does not)"
            if (Test-Path (Join-Path $Root $path)) {
                Write-Host "   note: the file DOES exist in the work tree -- it was excluded, or the listing was short"
            }
            $failed = $true
        }
    }

    # ---------------------------------------------------------------- line endings
    #
    # Third application of the same idea as the two checks above: verify the
    # bytes that actually shipped, not the declaration in the repo.
    #
    # `.gitattributes` opens with `* text=auto eol=lf`, yet the measured a55
    # delivery package had 711 of 832 files on CRLF or mixed endings --
    # including `.github/workflows/ci.yml`, which is explicitly on that
    # whitelist. The packing scripts were not at fault: `.gitattributes` is a
    # checkout-time rule, git does not rewrite a work tree that predates it,
    # and both scripts copy the work tree byte for byte. The rule was declared,
    # never took effect, and nothing noticed -- the package still printed OK.
    #
    # Three things genuinely break: compose `env_file` reads `\r` into a
    # password, Dockerfile backslash continuations snap, `.dockerignore`
    # entries stop matching. Full reasoning at the top of `.gitattributes`.
    #
    # The judgement itself lives in `tools/normalize_eol.py`. Rewriting it here
    # in PowerShell is exactly the list-divergence this repo has been bitten by
    # twice, and this particular divergence would show up as "the Linux package
    # is clean, the Windows one carries CRLF" -- the very scenario the gate
    # exists to prevent.
    $eolChecker = Join-Path $Root 'tools/normalize_eol.py'
    $python = Find-RunnablePython

    if (-not $python -or -not (Test-Path -LiteralPath $eolChecker)) {
        # Fail closed, do not skip. Skipping ships a CRLF package that prints
        # OK, while "this machine has no python" stays invisible -- which is
        # the "a gate that looks like it is running" failure this whole script
        # was written to prevent. The repo already requires Python for tests
        # and the delivery self-check; missing it on the packing box is an
        # environment problem, not grounds for silent passage.
        Write-Error 'Cannot run the line-ending check; refusing to emit a package.' -ErrorAction Continue
        if (-not $python) { Write-Host '   no runnable python/py/python3 on PATH' }
        if (-not (Test-Path -LiteralPath $eolChecker)) { Write-Host "   missing $eolChecker" }
        $failed = $true
    }
    else {
        $eolDir = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
        try {
            [System.IO.Compression.ZipFile]::ExtractToDirectory($Out, $eolDir)
            & $python $eolChecker --check $eolDir --quiet
            if ($LASTEXITCODE -ne 0) {
                Write-Host '   the files above came from the freshly built package, not the work tree.'
                Write-Host "   run `"$python tools/normalize_eol.py --write .`" in the work tree, then repack."
                $failed = $true
            }
        }
        catch {
            Write-Error "Line-ending check could not read the package: $_" -ErrorAction Continue
            $failed = $true
        }
        finally {
            if (Test-Path -LiteralPath $eolDir) {
                Remove-Item -LiteralPath $eolDir -Recurse -Force -ErrorAction SilentlyContinue
            }
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
