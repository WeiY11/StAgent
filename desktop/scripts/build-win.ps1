#requires -Version 5.1
<#
.SYNOPSIS
  Build LeAgent Desktop for Windows (x64).

.DESCRIPTION
  Downloads portable Python + uv, stages the backend payload, builds the
  frontend (Vite), compiles the Electron main process, and runs
  electron-builder to produce an NSIS installer.

.PARAMETER Version
  Semantic version string baked into the installer. Default: read from electron/package.json.

.PARAMETER SkipRuntime
  Skip downloading python-build-standalone and uv.

.PARAMETER SkipBackendPayload
  Skip copying the backend source tree.

.PARAMETER SkipFrontend
  Skip the frontend npm ci / build step.

.PARAMETER SkipCompileall
  Skip pre-compiling .pyc bytecode for bundled Python packages.

.PARAMETER Channel
  Release channel embedded in the package name: "stable" or "beta".
#>
param(
  [string]$Version = "",
  [switch]$SkipRuntime,
  [switch]$SkipBackendPayload,
  [switch]$SkipFrontend,
  [switch]$SkipCompileall,
  [ValidateSet("stable","beta")]
  [string]$Channel = "stable"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$DesktopElectron = Join-Path $Repo "desktop\electron"
if (-not $Version) {
  $Version = (Get-Content (Join-Path $DesktopElectron "package.json") -Raw | ConvertFrom-Json).version
}

function Remove-PathWithRetry {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [int]$Attempts = 5
  )

  if (-not (Test-Path -LiteralPath $Path)) { return }

  for ($i = 1; $i -le $Attempts; $i++) {
    try {
      Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
      return
    } catch {
      if ($i -eq $Attempts) {
        throw "Unable to remove '$Path'. Close LeAgent, Explorer windows, terminals, or antivirus scans using this folder, then retry. Last error: $_"
      }
      Start-Sleep -Seconds $i
    }
  }
}

function Clear-WinPackOutput {
  $distPack = Join-Path $DesktopElectron "dist-pack"
  Remove-PathWithRetry (Join-Path $distPack "win-unpacked.tmp")
  Remove-PathWithRetry (Join-Path $distPack "win-unpacked")
}

# Tracks only the exclusions this script added, so cleanup never removes
# exclusions the user configured beforehand.
$script:AddedDefenderExclusions = @()

function Test-IsAdministrator {
  try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  } catch {
    return $false
  }
}

function Get-ElectronBuilderCacheDir {
  # Mirrors app-builder-lib getCacheDirectory(): ELECTRON_BUILDER_CACHE wins when
  # it is an absolute path, otherwise Defender-relevant cache lives under LOCALAPPDATA.
  $override = $env:ELECTRON_BUILDER_CACHE
  if ($override -and [System.IO.Path]::IsPathRooted($override)) { return $override }
  if ($env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA "electron-builder\Cache") }
  return $null
}

function Add-WinBuildDefenderExclusions {
  # Windows Defender real-time scanning transiently locks freshly-extracted
  # executables, making electron-builder's atomic directory rename fail with EPERM
  # (both the dist-pack win-unpacked stage and the NSIS/7zip toolset cache stage).
  # Temporary path exclusions remove the lock at the root. Non-fatal on failure:
  # the build still runs and relies on rename retries.
  if (-not (Get-Command Add-MpPreference -ErrorAction SilentlyContinue)) {
    Write-Warning "Add-MpPreference unavailable (Defender absent or third-party AV); skipping exclusions."
    return
  }
  if (-not (Test-IsAdministrator)) {
    Write-Warning "Not elevated: skipping Defender exclusions. Run this script as Administrator to avoid EPERM rename stalls."
    return
  }

  $paths = @((Join-Path $DesktopElectron "dist-pack"), (Get-ElectronBuilderCacheDir)) | Where-Object { $_ }
  $existing = @((Get-MpPreference).ExclusionPath)
  foreach ($p in $paths) {
    if (-not (Test-Path -LiteralPath $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
    if ($existing -contains $p) { continue }  # leave user-configured exclusions untouched
    try {
      Add-MpPreference -ExclusionPath $p -ErrorAction Stop
      $script:AddedDefenderExclusions += $p
      Write-Host "  + Defender exclusion: $p"
    } catch {
      Write-Warning "Failed to add Defender exclusion '$p': $_"
    }
  }
}

function Remove-WinBuildDefenderExclusions {
  if (-not $script:AddedDefenderExclusions -or $script:AddedDefenderExclusions.Count -eq 0) { return }
  foreach ($p in $script:AddedDefenderExclusions) {
    try {
      Remove-MpPreference -ExclusionPath $p -ErrorAction Stop
      Write-Host "  - Removed Defender exclusion: $p"
    } catch {
      Write-Warning "Failed to remove Defender exclusion '$p': $_  (remove it manually via Windows Security if needed)"
    }
  }
  $script:AddedDefenderExclusions = @()
}

Write-Host "============================================"
Write-Host "  LeAgent Desktop — Windows Build"
Write-Host "  Version : $Version"
Write-Host "  Channel : $Channel"
Write-Host "============================================"

# ── 1. Runtime (Python + uv) ──
# Icons run after Electron npm ci (sharp is a desktop/electron dep).
if (-not $SkipRuntime) {
  Write-Host "`n==> prepare-runtime.mjs --platform win-x64"
  node (Join-Path $PSScriptRoot "prepare-runtime.mjs") --platform win-x64
  if ($LASTEXITCODE -ne 0) { throw "prepare-runtime.mjs failed (exit $LASTEXITCODE)" }
} else {
  Write-Warning "SkipRuntime: python-build-standalone + uv not refreshed."
}

# ── 2. Backend payload ──
if (-not $SkipBackendPayload) {
  Write-Host "`n==> prepare-backend-payload.mjs"
  node (Join-Path $PSScriptRoot "prepare-backend-payload.mjs")
  if ($LASTEXITCODE -ne 0) { throw "prepare-backend-payload.mjs failed (exit $LASTEXITCODE)" }
} else {
  Write-Warning "SkipBackendPayload: backend source tree not refreshed."
}

# ── 3. Frontend build ──
if (-not $SkipFrontend) {
  Write-Host "`n==> Frontend build (Vite)"
  $frontendDir = Join-Path $Repo "frontend"
  Push-Location $frontendDir
  try {
    # Relative /api/v1 — same-origin with backend-served SPA (any serverPort).
    $env:VITE_DESKTOP = "true"
    Remove-Item Env:\VITE_API_BASE_URL -ErrorAction SilentlyContinue
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed in frontend (exit $LASTEXITCODE)" }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed in frontend (exit $LASTEXITCODE)" }
  } finally {
    Remove-Item Env:\VITE_DESKTOP -ErrorAction SilentlyContinue
    Remove-Item Env:\VITE_API_BASE_URL -ErrorAction SilentlyContinue
    Pop-Location
  }
} else {
  Write-Warning "SkipFrontend: frontend/dist not rebuilt."
}

# ── 4. Compile bytecode ──
if (-not $SkipCompileall) {
  $payloadDir = Join-Path $DesktopElectron "resources\backend-payload"
  $runtimePy = Join-Path $DesktopElectron "resources\runtime\win-x64\python\python.exe"
  $runtimeWorks = $false
  if (Test-Path $runtimePy) {
    try {
      & $runtimePy --version *> $null
      $runtimeWorks = ($LASTEXITCODE -eq 0)
    } catch {
      $runtimeWorks = $false
    }
  }

  if ($runtimeWorks) {
    Write-Host "`n==> compileall backend payload"
    $leagentPkg = Join-Path $payloadDir "leagent"
    if (Test-Path $leagentPkg) {
      & $runtimePy -m compileall -q $leagentPkg
      if ($LASTEXITCODE -ne 0) { Write-Warning "compileall exited $LASTEXITCODE (non-fatal)" }
    }
  } else {
    Write-Warning "Compatible bundled Python not found at $runtimePy — skipping compileall."
  }
} else {
  Write-Warning "SkipCompileall: .pyc cache not refreshed."
}

# ── 5. Electron build + pack ──
Write-Host "`n==> Electron npm ci + build + pack"
Push-Location $DesktopElectron
try {
  npm ci
  if ($LASTEXITCODE -ne 0) { throw "npm ci failed in desktop/electron (exit $LASTEXITCODE)" }

  # npm ci restores the pristine app-builder-lib; re-apply the EPERM rename-retry
  # patch so extraction survives transient antivirus locks even without elevation.
  Write-Host "`n==> patch-electron-builder.mjs (EPERM rename retry)"
  node (Join-Path $PSScriptRoot "patch-electron-builder.mjs")
  if ($LASTEXITCODE -ne 0) { Write-Warning "patch-electron-builder.mjs exited $LASTEXITCODE (non-fatal)" }

  Write-Host "`n==> make-icons.mjs"
  $iconScript = Join-Path $PSScriptRoot "make-icons.mjs"
  try {
    node $iconScript
  } catch {
    Write-Warning "Icon generation failed: $_  (non-fatal)"
  }

  npm run build
  if ($LASTEXITCODE -ne 0) { throw "npm run build (tsc) failed (exit $LASTEXITCODE)" }

  npm test
  if ($LASTEXITCODE -ne 0) { throw "npm test failed (exit $LASTEXITCODE)" }

  $env:VERSION = $Version
  Clear-WinPackOutput

  # Add temporary Defender exclusions for the output + toolset-cache dirs to avoid
  # transient EPERM rename failures during Electron/NSIS extraction. Removed in finally.
  Add-WinBuildDefenderExclusions

  $maxBuildAttempts = 2
  for ($attempt = 1; $attempt -le $maxBuildAttempts; $attempt++) {
    # --publish never: release.yml attaches installers via softprops; without this,
    # a git tag triggers implicit GitHub publish and fails when GH_TOKEN is unset.
    npx electron-builder --win --x64 --publish never --config electron-builder.yml "--c.extraMetadata.version=$Version"
    if ($LASTEXITCODE -eq 0) { break }
    if ($attempt -eq $maxBuildAttempts) { throw "electron-builder failed (exit $LASTEXITCODE)" }

    Write-Warning "electron-builder failed (exit $LASTEXITCODE); clearing Windows pack output and retrying once."
    Clear-WinPackOutput
    Start-Sleep -Seconds 2
  }
} finally {
  Remove-WinBuildDefenderExclusions
  Pop-Location
}

# ── 7. Report ──
Write-Host "`n==> Build complete"
$distPack = Join-Path $DesktopElectron "dist-pack"
$setup = Get-ChildItem -Path $distPack -Filter "LeAgent-Setup-*.exe" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if ($setup) {
  $h = Get-FileHash -Path $setup.FullName -Algorithm SHA256
  Write-Host ("Installer : {0}" -f $setup.Name)
  Write-Host ("Size      : {0:N0} bytes ({1:N1} MB)" -f $setup.Length, ($setup.Length / 1MB))
  Write-Host ("SHA-256   : {0}" -f $h.Hash)
} else {
  Write-Host "Artifacts under: $distPack"
  Get-ChildItem $distPack -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("  {0} ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) }
}
