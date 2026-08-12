<#
.SYNOPSIS
  PrivateGPT Uploader — ziehe deutsche Text-/PDF-Dateien auf das Fenster und
  lies sie in die PrivateGPT-Collection "test_de_lang" ein (RAG).

.DESCRIPTION
  - Drag & Drop beliebiger .txt- und .pdf-Dateien (auch mehrere gleichzeitig)
    auf das Fenster, oder waehle sie per Klick aus.
  - "Einlesen" laedt jede Datei ueber die PrivateGPT-Ingest-API hoch
    (POST /v1/artifacts/ingest, Collection test_de_lang).
  - Zeigt Status und Ergebnis je Datei an.

.VORRAUSSETZUNG
  private-gpt muss laufen (start-private-gpt.ps1 -Action Start).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File private-gpt-upload.ps1
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$BaseUrl    = "http://localhost:8080"
$Collection = "test_de_lang"

# ---------------------------------------------------------------------------
# UI-Aufbau
# ---------------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text           = "PrivateGPT Uploader  -  $Collection"
$form.Size           = New-Object System.Drawing.Size(560, 470)
$form.StartPosition  = "CenterScreen"
$form.MinimumSize    = New-Object System.Drawing.Size(480, 400)
$form.AllowDrop      = $true

# Drop-Zone
$dropZone = New-Object System.Windows.Forms.Label
$dropZone.Location = New-Object System.Drawing.Point(16, 16)
$dropZone.Size     = New-Object System.Drawing.Size(512, 120)
$dropZone.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$dropZone.TextAlign   = [System.Drawing.ContentAlignment]::MiddleCenter
$dropZone.BackColor   = [System.Drawing.Color]::FromArgb(245, 245, 245)
$dropZone.Text = "Dateien hierher ziehen (.txt / .pdf)`n`noder auf ""Dateien waehlen"" klicken"
$form.Controls.Add($dropZone)

# Datei-Liste
$fileList = New-Object System.Windows.Forms.ListBox
$fileList.Location = New-Object System.Drawing.Point(16, 150)
$fileList.Size     = New-Object System.Drawing.Size(512, 180)
$form.Controls.Add($fileList)

# Status-Label
$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Location = New-Object System.Drawing.Point(16, 340)
$statusLabel.Size     = New-Object System.Drawing.Size(512, 40)
$statusLabel.Text     = "Bereit. $Collection"
$form.Controls.Add($statusLabel)

# Buttons
$btnAdd = New-Object System.Windows.Forms.Button
$btnAdd.Location = New-Object System.Drawing.Point(16, 390)
$btnAdd.Size     = New-Object System.Drawing.Size(140, 32)
$btnAdd.Text     = "Dateien waehlen..."
$form.Controls.Add($btnAdd)

$btnClear = New-Object System.Windows.Forms.Button
$btnClear.Location = New-Object System.Drawing.Point(164, 390)
$btnClear.Size     = New-Object System.Drawing.Size(100, 32)
$btnClear.Text     = "Leeren"
$form.Controls.Add($btnClear)

$btnIngest = New-Object System.Windows.Forms.Button
$btnIngest.Location = New-Object System.Drawing.Point(272, 390)
$btnIngest.Size     = New-Object System.Drawing.Size(120, 32)
$btnIngest.Text     = "Einlesen"
$btnIngest.BackColor = [System.Drawing.Color]::LightGreen
$form.Controls.Add($btnIngest)

$btnExit = New-Object System.Windows.Forms.Button
$btnExit.Location = New-Object System.Drawing.Point(400, 390)
$btnExit.Size     = New-Object System.Drawing.Size(128, 32)
$btnExit.Text     = "Beenden"
$form.Controls.Add($btnExit)

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
$script:Files = New-Object System.Collections.ArrayList

function Update-FileList {
    $fileList.Items.Clear()
    foreach ($f in $script:Files) { [void]$fileList.Items.Add($f) }
    $statusLabel.Text = "$($script:Files.Count) Datei(en) geladen - $Collection"
}

function Add-Files {
    param([string[]]$Paths)
    foreach ($p in $Paths) {
        $ext = [System.IO.Path]::GetExtension($p).ToLower()
        if ($ext -ne ".txt" -and $ext -ne ".pdf") {
            $statusLabel.Text = "Uebersprungen (kein txt/pdf): $(Split-Path $p -Leaf)"
            continue
        }
        if (-not $script:Files.Contains($p)) { [void]$script:Files.Add($p) }
    }
    Update-FileList
}

function Test-Server {
    try {
        $h = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec 5
        return ($h.status -eq "ok")
    } catch { return $false }
}

function Invoke-IngestFile {
    param([string]$FilePath)
    $fileName = Split-Path $FilePath -Leaf
    $artifact = [System.IO.Path]::GetFileNameWithoutExtension($fileName)
    $artifact = ($artifact -replace '[^a-zA-Z0-9_-]', '_')
    $bytes = [System.IO.File]::ReadAllBytes($FilePath)
    $b64   = [System.Convert]::ToBase64String($bytes)

    $body = @{
        artifact   = $artifact
        collection = $Collection
        input      = @{ type = "file"; value = $b64 }
        metadata   = @{ file_name = $fileName; quelle = "Drag&Drop Upload" }
    } | ConvertTo-Json -Depth 4

    $resp = Invoke-RestMethod -Uri "$BaseUrl/v1/artifacts/ingest" `
        -Method Post -ContentType "application/json" -Body $body -TimeoutSec 600
    return $resp
}

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
$form.Add_DragEnter({
    param($sender, $e)
    if ($e.Data.GetDataPresent([System.Windows.Forms.DataFormats]::FileDrop)) {
        $e.Effect = [System.Windows.Forms.DragDropEffects]::Copy
    }
})

$form.Add_DragDrop({
    param($sender, $e)
    $paths = $e.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop)
    Add-Files -Paths $paths
})

$btnAdd.Add_Click({
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Multiselect = $true
    $dlg.Filter = "Text- und PDF-Dateien (*.txt;*.pdf)|*.txt;*.pdf|Alle Dateien (*.*)|*.*"
    if ($dlg.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK) {
        Add-Files -Paths $dlg.FileNames
    }
})

$btnClear.Add_Click({ $script:Files.Clear(); Update-FileList })

$btnExit.Add_Click({ $form.Close() })

$btnIngest.Add_Click({
    if ($script:Files.Count -eq 0) { $statusLabel.Text = "Keine Dateien gewaehlt."; return }
    if (-not (Test-Server)) {
        $statusLabel.Text = "FEHLER: private-gpt nicht erreichbar ($BaseUrl). Starte es zuerst."
        return
    }
    $btnIngest.Enabled = $false
    $ok = 0; $fail = 0
    foreach ($f in $script:Files) {
        $statusLabel.Text = "Lese ein: $(Split-Path $f -Leaf) ..."
        $statusLabel.Refresh()
        [System.Windows.Forms.Application]::DoEvents()
        try {
            Invoke-IngestFile -FilePath $f | Out-Null
            $ok++
            $statusLabel.Text = "OK: $(Split-Path $f -Leaf)"
        } catch {
            $fail++
            $statusLabel.Text = "FEHLER bei $(Split-Path $f -Leaf): $($_.Exception.Message)"
        }
        $statusLabel.Refresh()
        [System.Windows.Forms.Application]::DoEvents()
    }
    $statusLabel.Text = "Fertig: $ok OK, $fail Fehler  ($Collection)"
    $btnIngest.Enabled = $true
})

$form.Add_Shown({ $form.Activate() })

[System.Windows.Forms.Application]::Run($form)
