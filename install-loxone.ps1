$ErrorActionPreference = "Stop"

$version = "15050304"
$zipUrl = "https://updatefiles.loxone.com/LoxConfig/LoxoneConfigSetup_$version.zip"
$zipPath = "C:\LoxoneConfig.zip"
$extractPath = "C:\LoxoneInstall"

Write-Host "Lade Loxone Config v$version von $zipUrl ..."
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath

Write-Host "Entpacke ZIP nach $extractPath ..."
Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force

$installer = Join-Path $extractPath "LoxoneConfigSetup.exe"
Write-Host "Starte Silent-Installation von $installer ..."
Start-Process -FilePath $installer -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-" -Wait

Write-Host "✅ Loxone Config $version installiert."
