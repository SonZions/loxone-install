$ErrorActionPreference = "Stop"

Write-Host "Ermittle aktuelle Loxone Config Version ..."
$updateXml = Invoke-WebRequest -Uri "https://update.loxone.com/updatecheck.xml" -UseBasicParsing
[xml]$xml = $updateXml.Content

$version = $xml.Miniserversoftware.Release.Version
$zipUrl  = $xml.Miniserversoftware.Release.Path
$zipPath = "C:\LoxoneConfig.zip"
$extractPath = "C:\LoxoneInstall"

Write-Host "Neueste Version: $version"
Write-Host "Lade von $zipUrl ..."
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath

Write-Host "Entpacke ZIP nach $extractPath ..."
Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force

$installer = Join-Path $extractPath "LoxoneConfigSetup.exe"
Write-Host "Starte Silent-Installation von $installer ..."
Start-Process -FilePath $installer -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-" -Wait

Write-Host "Loxone Config $version erfolgreich installiert."
