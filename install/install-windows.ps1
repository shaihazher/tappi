# tappi installer for Windows
# Usage: irm https://raw.githubusercontent.com/shaihazher/tappi/main/install/install-windows.ps1 | iex
$ErrorActionPreference = "Stop"

$VenvDir = "$env:USERPROFILE\.tappi-venv"
$MinPy = [version]"3.10"

Write-Host "`n🐍 tappi installer for Windows" -ForegroundColor Cyan
Write-Host "──────────────────────────────" -ForegroundColor DarkGray

# --- Step 1: Find Python ---
$PythonCmd = $null
foreach ($cmd in @("python3", "python", "py")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
        if ($ver -and ([version]$ver -ge $MinPy)) {
            $PythonCmd = $cmd
            Write-Host "✓ Found Python $ver" -ForegroundColor Green
            break
        }
    } catch {}
}

# --- Step 2: Install Python if missing ---
if (-not $PythonCmd) {
    Write-Host "⚠ Python $MinPy+ not found. Installing..." -ForegroundColor Yellow

    # Try winget first
    $hasWinget = Get-Command winget -ErrorAction SilentlyContinue
    if ($hasWinget) {
        Write-Host "  Installing via winget..."
        winget install Python.Python.3.13 --accept-source-agreements --accept-package-agreements
    } else {
        # Fallback: download installer
        Write-Host "  Downloading Python 3.13 installer..."
        $installerUrl = "https://www.python.org/ftp/python/3.13.2/python-3.13.2-amd64.exe"
        $installerPath = "$env:TEMP\python-installer.exe"
        Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath
        Write-Host "  Running installer (silent, adds to PATH)..."
        Start-Process -Wait -FilePath $installerPath -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0"
        Remove-Item $installerPath -ErrorAction SilentlyContinue
    }

    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "Machine")

    foreach ($cmd in @("python3", "python", "py")) {
        try {
            $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
            if ($ver -and ([version]$ver -ge $MinPy)) {
                $PythonCmd = $cmd
                break
            }
        } catch {}
    }

    if (-not $PythonCmd) {
        Write-Host "❌ Python installation failed. Install Python 3.10+ from python.org and re-run." -ForegroundColor Red
        exit 1
    }
    Write-Host "✓ Installed Python $ver" -ForegroundColor Green
}

# --- Step 3: Create venv ---
Write-Host "📦 Creating virtual environment at $VenvDir..."
& $PythonCmd -m venv $VenvDir

# Activate
$ActivateScript = Join-Path $VenvDir "Scripts\Activate.ps1"
& $ActivateScript

# --- Step 4: Upgrade pip & install tappi ---
Write-Host "⬆️  Upgrading pip..."
pip install --upgrade pip -q
Write-Host "📥 Installing tappi..."
pip install tappi

# --- Step 5: Profile integration ---
$ActivateLine = ". `"$ActivateScript`""
$ProfilePath = $PROFILE.CurrentUserCurrentHost

if (-not (Test-Path $ProfilePath)) {
    New-Item -Path $ProfilePath -ItemType File -Force | Out-Null
}

if (-not (Select-String -Path $ProfilePath -Pattern ([regex]::Escape($ActivateLine)) -Quiet -ErrorAction SilentlyContinue)) {
    Add-Content -Path $ProfilePath -Value "`n# tappi virtual environment"
    Add-Content -Path $ProfilePath -Value $ActivateLine
    Write-Host "✓ Added activation to PowerShell profile" -ForegroundColor Green
}

# --- Step 6: Create desktop launcher ---
$LauncherUrl = "https://raw.githubusercontent.com/shaihazher/tappi/main/install/launch-windows.bat"
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$LauncherPath = Join-Path $DesktopPath "Launch tappi.bat"
Invoke-WebRequest -Uri $LauncherUrl -OutFile $LauncherPath
Write-Host "✓ Created 'Launch tappi' on Desktop" -ForegroundColor Green

Write-Host ""
Write-Host "──────────────────────────────" -ForegroundColor DarkGray
Write-Host "✅ tappi installed!" -ForegroundColor Green
Write-Host ""
Write-Host "   Double-click 'Launch tappi' on your Desktop to start."
Write-Host "   Pick your AI provider in the Settings page that opens."
