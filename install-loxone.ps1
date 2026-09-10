#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Version = "latest"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Version = $Version.Trim()
if (-not $Version) { $Version = "latest" }
if ($Version -ne "latest" -and $Version -cnotmatch '\A[0-9]{8}\z') {
    throw "Version muss 'latest' oder eine achtstellige Buildnummer sein (z.B. 17020828)."
}

if ($Version -eq "latest") {
    Write-Host "Ermittle aktuelles Loxone Config Release ..."
    $updateXml = Invoke-WebRequest -Uri "https://update.loxone.com/updatecheck.xml" -UseBasicParsing -TimeoutSec 60
    [xml]$xml = $updateXml.Content
    $release = $xml.SelectSingleNode('/Miniserversoftware/Release')
    if (-not $release -or -not $release.GetAttribute('Version') -or -not $release.GetAttribute('Path')) {
        throw "Der Loxone Updatefeed enthaelt kein gueltiges Release."
    }
    $resolvedVersion = $release.GetAttribute('Version')
    # Der offizielle Feed liefert noch HTTP-Links; Downloads erfolgen ueber HTTPS.
    $zipUrl = $release.GetAttribute('Path') -replace '^http:', 'https:'
} else {
    $resolvedVersion = $Version
    $zipUrl = "https://updatefiles.loxone.com/LoxConfig/LoxoneConfigSetup_$Version.zip"
}

if ($zipUrl -cnotmatch '\Ahttps://updatefiles\.loxone\.com/LoxConfig/LoxoneConfigSetup_[0-9]{8}\.zip\z') {
    throw "Ungueltiger Loxone Config Downloadpfad im Updatefeed: $zipUrl"
}

# Ein eigenes Verzeichnis verhindert, dass eine alte EXE erneut installiert wird.
$workPath = Join-Path ([IO.Path]::GetTempPath()) ("LoxoneInstall-" + [guid]::NewGuid().ToString('N'))
$zipPath = Join-Path $workPath "LoxoneConfig.zip"
$extractPath = Join-Path $workPath "extracted"

try {
    New-Item -ItemType Directory -Path $workPath | Out-Null
    Write-Host "Loxone Config Version: $resolvedVersion"
    Write-Host "Lade von $zipUrl ..."
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing

    Write-Host "Entpacke ZIP nach $extractPath ..."
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
    $installers = @(Get-ChildItem -Path $extractPath -Filter "LoxoneConfigSetup*.exe" -File -Recurse)
    if ($installers.Count -ne 1) {
        throw "Erwartet wurde genau eine LoxoneConfigSetup*.exe im ZIP; gefunden: $($installers.Count)."
    }

    $installer = $installers[0].FullName
    Write-Host "Starte Silent-Installation von $installer ..."
    $process = Start-Process -FilePath $installer -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-" -Wait -PassThru
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "Loxone Config Installation fehlgeschlagen (Exitcode $($process.ExitCode))."
    }
    if ($process.ExitCode -eq 3010) {
        Write-Warning "Die Installation erfordert einen Neustart. Es wird nicht automatisch neu gestartet."
    }
    Write-Host "Loxone Config $resolvedVersion erfolgreich installiert."
} finally {
    Remove-Item -LiteralPath $workPath -Recurse -Force -ErrorAction SilentlyContinue
}
