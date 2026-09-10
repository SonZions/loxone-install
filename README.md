# Loxone Config installieren

Das Skript installiert standardmaessig das aktuelle **Release** aus dem offiziellen
[Loxone Updatefeed](https://update.loxone.com/updatecheck.xml). Beta-Versionen werden
nicht ausgewaehlt. Am 10.09.2026 liefert der Feed **17.2.08.28** (`17020828`).

In Windows PowerShell ab 5.1 **als Administrator** ausfuehren:

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/SonZions/loxone-install/main/install-loxone.ps1" -OutFile "$env:TEMP\install-loxone.ps1" -UseBasicParsing
& "$env:TEMP\install-loxone.ps1"
```

Optional einen konkreten Build installieren:

```powershell
& "$env:TEMP\install-loxone.ps1" -Version 17020828
```

Downloads erfolgen ueber HTTPS. Das ZIP wird in ein neues temporaeres Verzeichnis
entpackt; auch versionierte Installer-Dateinamen und Unterordner werden gefunden.
Fehler im Feed, beim Download oder bei der Installation brechen das Skript ab.
Ein erforderlicher Windows-Neustart wird gemeldet und nicht automatisch ausgefuehrt.

Das Skript kann auch in einer bestehenden Windows-VM gestartet werden. Es installiert
die Windows-Anwendung; es aktualisiert keine Miniserver-Firmware. Es richtet keinen
regelmaessigen Updatejob ein.

VmManager verwendet dieses Skript ueber eine feste Git-Commit-URL. Die Skriptversion
ist damit festgelegt, die zu installierende Config-Version wird bei jedem Aufruf
mit `latest` neu aus dem Release-Feed ermittelt.

## Tests

```powershell
pwsh -NoProfile -File tests/install-loxone.Tests.ps1
```

Alternativ `powershell.exe` statt `pwsh` verwenden. Die Tests simulieren Feed,
ZIP-Download und Installer-Prozess; es wird keine Software installiert.
Mit `-UpdateFeedPath <datei.xml>` kann zusaetzlich ein gespeicherter echter Updatefeed
geprueft werden.
