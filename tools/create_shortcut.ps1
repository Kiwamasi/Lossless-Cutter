# Creates a "Video Cutter" shortcut (no console window, custom icon) in the project folder.
#   -Desktop     also put one on the desktop
#   -StartMenu   also add it to the Start menu (searchable with the Windows key)
param([switch]$Desktop, [switch]$StartMenu)

$root = Split-Path $PSScriptRoot -Parent
$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { Write-Error "pythonw.exe not found - install Python and add it to PATH first."; exit 1 }

$targets = @(Join-Path $root "Video Cutter.lnk")
if ($Desktop)   { $targets += Join-Path ([Environment]::GetFolderPath("Desktop")) "Video Cutter.lnk" }
if ($StartMenu) { $targets += Join-Path ([Environment]::GetFolderPath("Programs")) "Video Cutter.lnk" }

$shell = New-Object -ComObject WScript.Shell
foreach ($path in $targets) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = '"' + (Join-Path $root "app\main.py") + '"'
    $lnk.WorkingDirectory = Join-Path $root "app"
    $lnk.IconLocation = (Join-Path $root "app\icon.ico") + ",0"
    $lnk.Description = "Quick lossless video cutter"
    $lnk.Save()
    Write-Output "Created $path"
}
