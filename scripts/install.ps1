# DeepCatalog one-shot installer for Windows (PowerShell).
#
#   irm https://github.com/dpastoetter/DeepCatalog/releases/latest/download/install.ps1 | iex
#
# Installs the latest *GitHub Release* tarball (same verified artifact as the
# in-app updater and install.sh), creates .venv, and installs dependencies.
#
# Optional environment variables:
#   DEEPCATALOG_DIR         install location (default: %USERPROFILE%\deepcatalog)
#   DEEPCATALOG_PORT        port printed in the run hint (default: 8080)
#   DEEPCATALOG_UPDATE_REPO   owner/repo (default: dpastoetter/DeepCatalog)

$ErrorActionPreference = "Stop"

function Write-Bold([string]$Message) {
    Write-Host $Message -ForegroundColor White
}

function Write-Ok([string]$Message) {
    Write-Host "  ✓ $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "  ! $Message" -ForegroundColor Yellow
}

function Write-Die([string]$Message) {
    Write-Host "  ✗ $Message" -ForegroundColor Red
    exit 1
}

function Get-PythonCommand {
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            if ($candidate -eq "py") {
                $ver = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $ver) {
                    return @{ Exe = "py"; Args = @("-3"); Version = $ver.Trim() }
                }
            } else {
                $ver = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $ver) {
                    return @{ Exe = $candidate; Args = @(); Version = $ver.Trim() }
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

$Repo = if ($env:DEEPCATALOG_UPDATE_REPO) { $env:DEEPCATALOG_UPDATE_REPO.Trim() } else { "dpastoetter/DeepCatalog" }
$InstallDir = if ($env:DEEPCATALOG_DIR) { $env:DEEPCATALOG_DIR.Trim() } else { Join-Path $env:USERPROFILE "deepcatalog" }
$Port = if ($env:DEEPCATALOG_PORT) { $env:DEEPCATALOG_PORT.Trim() } else { "8080" }
$releaseCommit = Join-Path $InstallDir ".release-commit"

Write-Bold "DeepCatalog installer"
Write-Host "  → $InstallDir"
Write-Host "  source: release"
Write-Host ""

$python = Get-PythonCommand
if (-not $python) {
    Write-Die "Python 3.10+ is required. Install from https://www.python.org/downloads/ or: winget install Python.Python.3.12"
}
$parts = $python.Version.Split(".")
$major = [int]$parts[0]
$minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Write-Die "Python 3.10+ required (found $($python.Version))"
}
Write-Ok "Python $($python.Version)"

$pdftoppm = Get-Command pdftoppm -ErrorAction SilentlyContinue
if ($pdftoppm) {
    Write-Ok "poppler (pdftoppm) — PDF OCR ready"
} else {
    Write-Warn "poppler not found — AI OCR for PDFs needs pdftoppm on PATH"
    Write-Warn "  winget search poppler   # or install a Poppler build and add it to PATH"
    Write-Warn "  https://github.com/oschwartz10612/poppler-windows/releases"
}

$tesseract = Get-Command tesseract -ErrorAction SilentlyContinue
if ($tesseract) {
    Write-Ok "tesseract — classical OCR fast path ready"
} else {
    Write-Warn "tesseract not found — scanned pages fall back to slower AI vision OCR"
    Write-Warn "  Install a UB Mannheim build and add tesseract to PATH"
    Write-Warn "  https://github.com/UB-Mannheim/tesseract/wiki"
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("deepcatalog-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null

try {
    Write-Bold "Fetching latest GitHub release"
    $api = "https://api.github.com/repos/$Repo/releases/latest"
    $headers = @{
        "Accept"     = "application/vnd.github+json"
        "User-Agent" = "DeepCatalog-installer"
    }
    try {
        $release = Invoke-RestMethod -Uri $api -Headers $headers
    } catch {
        Write-Die "Could not fetch $api — check network / repo name"
    }

    $archive = $null
    $sumsUrl = $null
    foreach ($asset in $release.assets) {
        $name = [string]$asset.name
        $url = [string]$asset.browser_download_url
        if (-not $url) { continue }
        $lower = $name.ToLowerInvariant()
        if ($name -in @("SHA256SUMS", "SHA256SUMS.txt", "checksums.txt")) {
            $sumsUrl = $url
        } elseif ($lower.StartsWith("deepcatalog-") -and ($lower.EndsWith(".tar.gz") -or $lower.EndsWith(".tgz"))) {
            $archive = @{ Name = $name; Url = $url; Digest = [string]$asset.digest }
        }
    }
    if (-not $archive) {
        Write-Die "latest release has no deepcatalog-*.tar.gz asset"
    }

    $tag = [string]$release.tag_name
    Write-Ok "release $tag ($($archive.Name))"

    $archivePath = Join-Path $tmp $archive.Name
    Invoke-WebRequest -Uri $archive.Url -OutFile $archivePath -Headers @{ "User-Agent" = "DeepCatalog-installer" }

    $expected = $null
    if ($sumsUrl) {
        $sumsPath = Join-Path $tmp "SHA256SUMS"
        Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsPath -Headers @{ "User-Agent" = "DeepCatalog-installer" }
        foreach ($line in Get-Content $sumsPath) {
            if ($line -match '^\s*([A-Fa-f0-9]{64})\s+\*?(.+?)\s*$') {
                $fileName = $Matches[2].Trim()
                if ($fileName -eq $archive.Name -or $fileName -eq "*$($archive.Name)") {
                    $expected = $Matches[1].ToLowerInvariant()
                    break
                }
            }
        }
    }
    if (-not $expected -and $archive.Digest -match '^sha256:([A-Fa-f0-9]{64})$') {
        $expected = $Matches[1].ToLowerInvariant()
    }
    if (-not $expected) {
        Write-Die "No SHA-256 available for $($archive.Name) — refusing unverified install"
    }

    $actual = (Get-FileHash -Algorithm SHA256 -Path $archivePath).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        Write-Die "SHA-256 mismatch for $($archive.Name) (expected $expected, got $actual)"
    }
    Write-Ok "SHA-256 verified"

    $extractRoot = Join-Path $tmp "extract"
    New-Item -ItemType Directory -Path $extractRoot | Out-Null
    tar -xzf $archivePath -C $extractRoot
    if ($LASTEXITCODE -ne 0) {
        Write-Die "tar extract failed — ensure Windows tar (bsdtar) is available"
    }

    $src = Get-ChildItem -Path $extractRoot -Directory | Select-Object -First 1
    if (-not $src) { Write-Die "unexpected tarball layout" }
    if (-not (Test-Path (Join-Path $src.FullName "pyproject.toml"))) {
        Write-Die "release archive missing pyproject.toml"
    }
    if (-not (Test-Path (Join-Path $src.FullName "app\main.py"))) {
        Write-Die "release archive missing app\main.py"
    }

    Write-Bold "Installing into $InstallDir"
    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir | Out-Null
    }

    $syncScript = @'
import shutil
import sys
from pathlib import Path

src = Path(sys.argv[1]).resolve()
dest = Path(sys.argv[2]).resolve()
protected = {"data", ".env", ".venv", "venv", ".git", "node_modules"}
dest.mkdir(parents=True, exist_ok=True)

new_files: set[str] = set()
for path in src.rglob("*"):
    if not path.is_file():
        continue
    rel = path.relative_to(src)
    if rel.parts and rel.parts[0] in protected:
        continue
    if rel.name == ".env":
        continue
    new_files.add(rel.as_posix())
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)

manifest = src / ".release-files"
old_manifest = dest / ".release-files"
previous: set[str] = set()
if old_manifest.is_file():
    previous = {
        line.strip()
        for line in old_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

obsolete = previous - new_files
for rel in sorted(obsolete):
    parts = Path(rel).parts
    if not parts or parts[0] in protected or Path(rel).name == ".env":
        continue
    target = dest / rel
    if target.is_file() and not target.is_symlink():
        target.unlink()
        parent = target.parent
        while parent != dest and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

if manifest.is_file():
    shutil.copy2(manifest, dest / ".release-files")
print(f"synced {len(new_files)} files; removed {len(obsolete)} stale paths")
'@
    $syncPath = Join-Path $tmp "sync_install.py"
    Set-Content -Path $syncPath -Value $syncScript -Encoding UTF8
    & $python.Exe (@($python.Args) + @($syncPath, $src.FullName, $InstallDir))
    if ($LASTEXITCODE -ne 0) { Write-Die "failed to sync release archive into $InstallDir" }
    Write-Ok "code synced from release archive"

    if (Test-Path $releaseCommit) {
        $manifestText = ((Get-Content $releaseCommit) -join " ")
        Write-Ok "installed $manifestText"
    } else {
        Write-Ok "installed $tag"
    }
} finally {
    if (Test-Path $tmp) {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

Set-Location $InstallDir

function Test-VenvUsable {
    $py = Join-Path $InstallDir ".venv\Scripts\python.exe"
    $activate = Join-Path $InstallDir ".venv\Scripts\Activate.ps1"
    return (Test-Path $py) -and (Test-Path $activate)
}

Write-Bold "Creating virtualenv"
if (Test-VenvUsable) {
    Write-Ok "reusing existing .venv"
} else {
    $venvPath = Join-Path $InstallDir ".venv"
    if (Test-Path $venvPath) {
        Write-Warn "existing .venv is incomplete — recreating"
        Remove-Item -Recurse -Force $venvPath
    }
    & $python.Exe (@($python.Args) + @("-m", "venv", $venvPath))
    if ($LASTEXITCODE -ne 0 -or -not (Test-VenvUsable)) {
        Write-Die "python -m venv failed — reinstall Python with the venv component enabled"
    }
    Write-Ok "created .venv"
}

$venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
Write-Ok "venv at $(Join-Path $InstallDir '.venv')"

Write-Bold "Installing Python packages"
& $venvPy -m pip install -U pip | Out-Null
& $venvPy -m pip install -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Die "pip install failed" }
Write-Ok "dependencies installed"

$envFile = Join-Path $InstallDir ".env"
$envExample = Join-Path $InstallDir ".env.example"
if (-not (Test-Path $envFile)) {
    Copy-Item $envExample $envFile
    Write-Ok "created .env from .env.example"
} else {
    Write-Ok ".env already present — left untouched"
}
# Restrict .env like OAuth credentials (owner-only); best-effort on NTFS.
& $venvPy -c "from pathlib import Path; import sys; from deepcatalog.env_permissions import harden_secret_file; r=harden_secret_file(Path(sys.argv[1]), fix=True); raise SystemExit(0 if (not r.get('was_insecure') or r.get('fixed') or not r.get('exists')) else 1)" $envFile
if ($LASTEXITCODE -ne 0) {
    Write-Warn "could not fully tighten .env permissions (mode 600)"
} else {
    Write-Ok ".env permissions set to owner-only (0600)"
}

foreach ($dir in @("data\inbox", "data\archive", "data\chroma")) {
    $path = Join-Path $InstallDir $dir
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
    }
}
Write-Ok "data directories ready"

$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollama) {
    Write-Ok "ollama CLI found — use Settings → Local Ollama for fully local models"
} else {
    Write-Warn "ollama not found — optional for fully local AI (https://ollama.com/download)"
}

Write-Host ""
Write-Bold "Install complete"
if (Test-Path $releaseCommit) {
    Write-Host ""
    Write-Host "  Release manifest:"
    Get-Content $releaseCommit | ForEach-Object { Write-Host "    $_" }
}

Write-Host @"

  Start the app (activate .venv first — do not use a system uvicorn):

    cd $InstallDir
    .\.venv\Scripts\Activate.ps1
    uvicorn app.main:app --host 127.0.0.1 --port $Port

  Then open http://localhost:$Port

  If Activate.ps1 is blocked, run once:
    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

  First-run tips:
    • Settings → AI provider — Sign in with ChatGPT, or click Local Ollama
    • Settings → Filing & scanning — point the inbox at your scan folder
    • Drop a PDF in Inbox and click Process inbox
    • Boot autostart is Linux-only (not available on Windows)

  Re-run this installer anytime to install the latest verified release:
    irm https://github.com/$Repo/releases/latest/download/install.ps1 | iex

  Uninstall (removes app code, .venv, local data/, and .env):
    Remove-Item -Recurse -Force "$InstallDir"

  That does not delete %USERPROFILE%\.codex\auth.json or archive folders
  outside the install directory.

"@
