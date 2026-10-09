<#
.SYNOPSIS
  Gör FLSync-scriptet och pianorulle-scripten synliga för FL Studio.

.DESCRIPTION
  Skapar en länk (directory junction) från FL Studios Hardware-mapp till
  fl\FLSync i det här projektet, så att ändringar i koden syns direkt i FL
  (ladda om scriptet i FL:s Script output-fönster). Kopierar också
  pianorulle-scripten för noter (fl\Piano roll scripts) till FL:s mapp för
  pianorulle-script; kör installationen igen när de har ändrats.

.PARAMETER Copy
  Kopiera FLSync-scriptet i stället för att länka.

.PARAMETER Uninstall
  Ta bort FLSync från FL Studio. Projektets filer rörs inte.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
param([switch]$Copy, [switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\fl\FLSync')).Path
$scripts = (Resolve-Path (Join-Path $PSScriptRoot '..\fl\Piano roll scripts')).Path
$settings = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Image-Line\FL Studio\Settings'
$hardware = Join-Path $settings 'Hardware'
$pianoRoll = Join-Path $settings 'Piano roll scripts'
$target = Join-Path $hardware 'FLSync'

if (-not (Test-Path $hardware)) {
    throw "Hittar inte FL Studios Hardware-mapp: $hardware"
}

$existing = Get-Item $target -ErrorAction SilentlyContinue
$isLink = $existing -and $existing.LinkType -eq 'Junction'

if ($Uninstall) {
    if ($existing) {
        if ($isLink) {
            cmd /c rmdir "$target"  # removes only the link, never the project files
        } else {
            Remove-Item $target -Recurse -Force
        }
        Write-Host "Borttaget: $target"
    } else {
        Write-Host 'FLSync-scriptet är inte installerat.'
    }
    foreach ($script in Get-ChildItem $scripts -Filter '*.pyscript') {
        $installed = Join-Path $pianoRoll $script.Name
        if (Test-Path -LiteralPath $installed) {
            Remove-Item -LiteralPath $installed
            Write-Host "Borttaget: $installed"
        }
    }
    return
}

if ($existing) {
    if (-not ($isLink -and (@($existing.Target) -contains $source))) {
        throw "$target finns redan. Kör med -Uninstall först om du vill ersätta det."
    }
    Write-Host "Redan installerat: $target -> $source"
} elseif ($Copy) {
    Copy-Item $source $target -Recurse
    Write-Host "Kopierat till $target"
} else {
    New-Item -ItemType Junction -Path $target -Target $source | Out-Null
    Write-Host "Länkat $target -> $source"
}

New-Item -ItemType Directory -Force -Path $pianoRoll | Out-Null
foreach ($script in Get-ChildItem $scripts -Filter '*.pyscript') {
    Copy-Item -LiteralPath $script.FullName -Destination $pianoRoll -Force
    Write-Host "Pianorulle-script: $(Join-Path $pianoRoll $script.Name)"
}
if (-not $existing) {
    Write-Host 'Starta om FL Studio om det är igång, så att scriptet dyker upp i MIDI-inställningarna.'
}
