$ErrorActionPreference = "Stop"
Write-Host "Hole aktuelle Loxone Config Version..."

# Lade XML mit den Versionen
$updateXml = Invoke-WebRequest -Uri "https://update.loxone.com/versions/update.xml" -UseBasicParsing
[xml]$xml = $updateXml.Content

# Hole aktuellste Version von Loxone Config
$latestConfig = $xml.SelectSingleNode("//Version[starts-with(Product, 'LoxoneConfig')][1]")
$version = $latestConfig.Number
$downloadUrl = "https://update.loxone.com/pub/software/LoxoneConfig/$version/LoxoneConfig.exe"

Write-Host "Lade LoxoneConfig $version herunter..."
Invoke-WebRequest -Uri $downloadUrl -OutFile "C:\LoxoneConfig.exe"

Write-Host "Installiere Loxone Config $version..."
Start-Process "C:\LoxoneConfig.exe" -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-" -Wait

Write-Host "✅ Loxone Config $version installiert."
