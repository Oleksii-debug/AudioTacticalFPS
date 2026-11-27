param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
$cursor = $output
while ($cursor) {
    if (Test-Path (Join-Path $cursor '.git')) { throw 'Private binaries must be built outside Git worktrees.' }
    $parent = Split-Path $cursor -Parent
    if ($parent -eq $cursor) { break }
    $cursor = $parent
}
New-Item -ItemType Directory -Force $output | Out-Null
$launcher = Join-Path $output 'ResearchLibrary.exe'
if (Test-Path $launcher) { throw 'Choose a fresh private output directory.' }
$toolchain = @{}
foreach ($name in @('cl.exe', 'link.exe', 'rc.exe')) {
    $file = (Get-Command $name -ErrorAction Stop).Source
    $toolchain[$name] = @{ sha256 = (Get-FileHash $file -Algorithm SHA256).Hash.ToLowerInvariant(); version = (Get-Item $file).VersionInfo.FileVersion }
}
Push-Location $PSScriptRoot
try {
    $resource = Join-Path $output 'launcher.res'
    $object = Join-Path $output 'launcher.obj'
    & rc.exe /nologo /c 65001 /fo $resource launcher.rc
    if ($LASTEXITCODE -ne 0) { throw "Resource compilation failed: $LASTEXITCODE" }
    & cl.exe /nologo /W4 /WX /utf-8 /O1 /Oi /GS- /Zl /Brepro /c launcher.c "/Fo$object"
    if ($LASTEXITCODE -ne 0) { throw "Launcher compilation failed: $LASTEXITCODE" }
    & link.exe /NOLOGO /SUBSYSTEM:WINDOWS /ENTRY:launcher_entry /NODEFAULTLIB /MANIFEST:NO /INCREMENTAL:NO /DYNAMICBASE /NXCOMPAT /HIGHENTROPYVA /Brepro "/OUT:$launcher" $object $resource kernel32.lib user32.lib shell32.lib
    if ($LASTEXITCODE -ne 0) { throw "Launcher linking failed: $LASTEXITCODE" }
    $childObject = Join-Path $output 'windows_child_probe.obj'
    $child = Join-Path $output 'windows_child_probe.exe'
    & cl.exe /nologo /W4 /WX /wd4505 /utf-8 /O1 /Oi /GS- /Zl /Brepro /c windows_child_probe.c "/Fo$childObject"
    if ($LASTEXITCODE -ne 0) { throw "Probe compilation failed: $LASTEXITCODE" }
    & link.exe /NOLOGO /SUBSYSTEM:WINDOWS /ENTRY:launcher_entry /NODEFAULTLIB /MANIFEST:NO /INCREMENTAL:NO /DYNAMICBASE /NXCOMPAT /HIGHENTROPYVA /Brepro "/OUT:$child" $childObject kernel32.lib shell32.lib
    if ($LASTEXITCODE -ne 0) { throw "Probe linking failed: $LASTEXITCODE" }
    $core = Join-Path $output 'launcher_core_test.exe'
    $coreObject = Join-Path $output 'launcher_core_test.obj'
    & cl.exe /nologo /W4 /WX /utf-8 test_launcher_core.c "/Fo$coreObject" "/Fe$core"
    if ($LASTEXITCODE -ne 0) { throw "Core test compilation failed: $LASTEXITCODE" }
    & $core
    if ($LASTEXITCODE -ne 0) { throw "Core tests failed: $LASTEXITCODE" }
    $peJson = & python probe_pe.py $launcher
    if ($LASTEXITCODE -ne 0) { throw "PE inspection failed: $LASTEXITCODE" }
    $sources = @{}
    foreach ($name in @('launcher.c','launcher_core.h','launcher.manifest','launcher.rc','windows_child_probe.c','build_msvc.ps1','probe_pe.py')) {
        $sources[$name] = (Get-FileHash $name -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $report = @{ schema = 1; target = 'Windows x64 / MSVC'; toolchain = $toolchain; source_sha256 = $sources; pe = ($peJson | ConvertFrom-Json); release_status = 'unsigned-private-candidate' }
    $report | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path $output 'build-evidence.json')
    $report | ConvertTo-Json -Depth 8
} finally { Pop-Location }
