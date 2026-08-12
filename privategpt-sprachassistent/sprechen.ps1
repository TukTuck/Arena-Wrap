<#
.SYNOPSIS
  Sprachausgabe (TTS): liest Text laut vor - mit moderner Neural-Stimme (Katja).

.DESCRIPTION
  Nutzt edge-tts (Microsoft Neural-Stimmen, wie in Edge) fuer natuerlichen Klang.
  Faellt automatisch auf die Windows-Stimme zurueck, wenn edge-tts nicht
  verfuegbar ist oder kein Internet besteht.

  Textquelle (in dieser Reihenfolge):
    1. Argument:  powershell -File sprechen.ps1 "Hallo Welt"
    2. Zwischenablage (wenn kein Argument)
    3. Eingabeaufforderung (wenn beides leer)

  Parameter:
    -Text       : Text, der vorgelesen werden soll
    -Voice      : Stimme (default: de-DE-KatjaNeural)
                  Alternativen: de-DE-ConradNeural (maennlich), de-DE-FlorianMultilingualNeural
    -ListVoices : Zeigt alle deutschen Neural-Stimmen an

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File sprechen.ps1 "Guten Morgen!"
#>
param(
    [string]$Text,
    [string]$Voice = "de-DE-KatjaNeural",
    [switch]$ListVoices
)

$EdgeTts = "C:\Users\Hansi\diktat\.venv\Scripts\edge-tts.exe"

if ($ListVoices) {
    if (Test-Path $EdgeTts) {
        Write-Host "Verfuegbare deutsche Neural-Stimmen (edge-tts):"
        & $EdgeTts --list-voices | Select-String "de-" | ForEach-Object { Write-Host "  - $($_.Line)" }
        return
    }
    Write-Host "edge-tts nicht gefunden - nur Windows-Stimmen:"
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.GetInstalledVoices() | ForEach-Object { Write-Host "  - $($_.VoiceInfo.Name)" }
    return
}

# Textquelle bestimmen
if (-not $Text) {
    try { $Text = Get-Clipboard -Raw -ErrorAction Stop } catch { $Text = "" }
    $Text = $Text.Trim()
    if (-not $Text) {
        $Text = Read-Host "Text zum Vorlesen"
    } else {
        Write-Host "Text aus Zwischenablage:" -ForegroundColor Gray
        Write-Host $Text
    }
}

# 1. Versuch: edge-tts (Neural-Stimme)
if (Test-Path $EdgeTts) {
    # Eindeutiger Dateiname pro Aufruf: der Media-Player haelt die mp3 sonst
    # offen, und eine ueberschriebene Datei wird beim zweiten Mal nicht mehr abgespielt.
    $stamp = Get-Date -Format "yyyyMMdd-HHmmssfff"
    $mp3 = Join-Path $env:TEMP ("pgpt-tts-{0}.mp3" -f $stamp)
    try {
        & $EdgeTts --voice $Voice --text $Text --write-media $mp3 2>$null
        if (Test-Path $mp3) {
            Write-Host "Lese vor ... (Stimme: $Voice)" -ForegroundColor DarkGray
            Start-Process $mp3
            return
        }
    } catch {
        Write-Host "[!] edge-tts fehlgeschlagen - Fallback auf Windows-Stimme" -ForegroundColor Yellow
    }
}

# 2. Fallback: Windows-System.Speech (funktioniert auch offline)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$de = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like "de-*" } | Select-Object -First 1
if ($de) { $synth.SelectVoice($de.VoiceInfo.Name) }
Write-Host "Lese vor ... (Stimme: $(if ($de) { $de.VoiceInfo.Name } else { 'Standard' }))" -ForegroundColor DarkGray
$synth.Speak($Text)
$synth.Dispose()
