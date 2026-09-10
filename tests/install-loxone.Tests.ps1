# Run with Windows PowerShell 5.1 or pwsh; no Azure VM or installer is started.
param([string]$UpdateFeedPath)
$ErrorActionPreference = 'Stop'
$script:InstallerScript = Join-Path $PSScriptRoot '../install-loxone.ps1'

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec)
    if ($OutFile) {
        $script:WorkPath = Split-Path $OutFile
        if ($script:DownloadFails) { throw 'Simulated download failure' }
        $script:DownloadedUrl = $Uri
        Set-Content -LiteralPath $OutFile -Value 'mock ZIP'
    } else {
        $script:FeedRequests++
        Assert-True ($Uri -eq 'https://update.loxone.com/updatecheck.xml') 'official release feed'
        return [pscustomobject]@{ Content = $script:Feed }
    }
}

function Expand-Archive {
    param($Path, $DestinationPath, [switch]$Force)
    $script:WorkPath = Split-Path $DestinationPath
    $nested = Join-Path $DestinationPath 'nested'
    New-Item -ItemType Directory -Path $nested -Force | Out-Null
    foreach ($name in $script:InstallerNames) {
        Set-Content -LiteralPath (Join-Path $nested $name) -Value 'mock EXE'
    }
}

function Start-Process {
    param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru)
    $script:Started = $FilePath
    Assert-True (Test-Path -LiteralPath $FilePath) 'installer exists'
    Assert-True ($Wait -and $PassThru) 'wait for the installer exit code'
    Assert-True ($ArgumentList -match '/NORESTART') 'no automatic reboot'
    return [pscustomobject]@{ ExitCode = $script:InstallerExitCode }
}

function Reset-Test {
    $script:Feed = '<Miniserversoftware><Release Version="17.2.08.28" Path="http://updatefiles.loxone.com/LoxConfig/LoxoneConfigSetup_17020828.zip"/><Beta Version="18.0.1.1" Path="http://updatefiles.loxone.com/LoxConfig/LoxoneConfigSetup_18000101.zip"/></Miniserversoftware>'
    $script:FeedRequests = 0
    $script:DownloadedUrl = $null
    $script:Started = $null
    $script:WorkPath = $null
    $script:DownloadFails = $false
    $script:InstallerExitCode = 0
    $script:InstallerNames = @('LoxoneConfigSetup_17020828.exe')
}

function Run-Installer {
    param([hashtable]$Arguments = @{}, [string]$ExpectedError)
    $failure = $null
    try { . $script:InstallerScript @Arguments } catch { $failure = $_.Exception.Message }
    if ($ExpectedError) {
        Assert-True ($failure -and $failure -like "*$ExpectedError*") "expected '$ExpectedError', got '$failure'"
    } else {
        Assert-True (-not $failure) "unexpected error: $failure"
    }
    if ($script:WorkPath) {
        Assert-True (-not (Test-Path -LiteralPath $script:WorkPath)) 'temporary files cleaned up'
    }
}

Reset-Test
Run-Installer
Assert-True ($script:FeedRequests -eq 1) 'default resolves the feed'
Assert-True ($script:DownloadedUrl -eq 'https://updatefiles.loxone.com/LoxConfig/LoxoneConfigSetup_17020828.zip') 'release, not beta, downloaded over HTTPS'
Assert-True ($script:Started -like '*LoxoneConfigSetup_17020828.exe') 'versioned nested installer found'

Reset-Test
$script:Feed = $script:Feed.Replace('17.2.08.28', '18.0.01.01').Replace('17020828', '18000101')
$script:InstallerNames = @('LoxoneConfigSetup_18000101.exe')
Run-Installer -Arguments @{ Version = ' LATEST ' }
Assert-True ($script:DownloadedUrl -like '*_18000101.zip') 'future releases are not pinned to 17'

Reset-Test
Run-Installer -Arguments @{ Version = ' 17020828 ' }
Assert-True ($script:FeedRequests -eq 0) 'explicit version skips the feed'
Assert-True ($script:DownloadedUrl -like '*_17020828.zip') 'explicit build selected'

Reset-Test
$script:InstallerNames = @('LoxoneConfigSetup.exe')
Run-Installer -Arguments @{ Version = '' }
Assert-True ($script:FeedRequests -eq 1) 'blank version defaults to latest'
Assert-True ($script:Started -like '*LoxoneConfigSetup.exe') 'unversioned installer still supported'

foreach ($invalid in @('beta', '17.2.8.28', '17020828;whoami', "17020828`nextra")) {
    Reset-Test
    Run-Installer -Arguments @{ Version = $invalid } -ExpectedError 'Version muss'
    Assert-True ($script:FeedRequests -eq 0 -and -not $script:Started) 'invalid version stops before network or installer'
}

foreach ($testFeed in @('<Miniserversoftware/>', '<Miniserversoftware><Release Version="17.2"/></Miniserversoftware>', '<not-xml')) {
    Reset-Test
    $script:Feed = $testFeed
    Run-Installer -ExpectedError '*'
    Assert-True (-not $script:DownloadedUrl -and -not $script:Started) 'invalid feed does not fall back to an old release'
}

Reset-Test
$script:Feed = $script:Feed.Replace('updatefiles.loxone.com', 'example.com')
Run-Installer -ExpectedError 'Ungueltiger Loxone Config Downloadpfad'
Assert-True (-not $script:DownloadedUrl) 'untrusted download rejected'

foreach ($count in @(0, 2)) {
    Reset-Test
    $script:InstallerNames = @()
    if ($count -eq 2) { $script:InstallerNames = @('LoxoneConfigSetup.exe', 'LoxoneConfigSetup_old.exe') }
    Run-Installer -ExpectedError 'Erwartet wurde genau eine'
    Assert-True (-not $script:Started) 'missing or ambiguous installer rejected'
}

Reset-Test
$script:DownloadFails = $true
Run-Installer -ExpectedError 'Simulated download failure'
Assert-True (-not $script:Started) 'download failure stops installation'

Reset-Test
$script:InstallerExitCode = 1
Run-Installer -ExpectedError 'Exitcode 1'

Reset-Test
$script:InstallerExitCode = 3010
Run-Installer

if ($UpdateFeedPath) {
    Reset-Test
    $script:Feed = Get-Content -LiteralPath $UpdateFeedPath -Raw
    Run-Installer
    Write-Host "Verified supplied update feed: $script:DownloadedUrl"
}

Write-Host 'All installer tests passed.'
