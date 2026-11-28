# Static local-only SAPI5 adapter. User text is JSON data, never PowerShell/SSML.
# Both native and WOW64 Windows PowerShell hosts use this same helper.
param([Parameter(Mandatory=$true)][string]$Request)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$synth = $null
try {
    $requestPath = [IO.Path]::GetFullPath($Request)
    if ((Get-Item -LiteralPath $requestPath).Length -gt 262144) { throw 'Request too large' }
    $data = [IO.File]::ReadAllText($requestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $directory = [IO.Path]::GetDirectoryName($requestPath)
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    if ($data.operation -eq 'voices') {
        $voices = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object {
            $voice = $_.VoiceInfo
            $vendor = ''
            if ($voice.AdditionalInfo.ContainsKey('Vendor')) { $vendor = $voice.AdditionalInfo['Vendor'] }
            @{id=$voice.Id; name=$voice.Name; language=$voice.Culture.Name; vendor=$vendor}
        })
        $runtime = @{system_speech_version=[System.Speech.Synthesis.SpeechSynthesizer].Assembly.GetName().Version.ToString();
                     powershell_version=$PSVersionTable.PSVersion.ToString(); bitness=([IntPtr]::Size * 8)}
        $reply = @{version=1; ok=$true; voices=$voices; runtime=$runtime}
    } elseif ($data.operation -eq 'synthesize') {
        if ($data.text -isnot [string] -or $data.text.Length -lt 1 -or $data.text.Length -gt 64000) {
            throw 'Invalid speech text'
        }
        $selected = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Id -eq $data.voice_id })
        if ($selected.Count -ne 1) { throw 'Voice is no longer available' }
        $synth.SelectVoice($selected[0].VoiceInfo.Name)
        if ($synth.Voice.Id -ne $data.voice_id) { throw 'Voice name is ambiguous' }
        if ($data.rate -lt -10 -or $data.rate -gt 10 -or $data.volume -lt 0 -or $data.volume -gt 100) {
            throw 'Invalid speech controls'
        }
        if ($data.sample_rate -ne 24000) { throw 'Unsupported sample format' }
        $synth.Rate = [int]$data.rate
        $synth.Volume = [int]$data.volume
        $output = Join-Path $directory 'speech.wav'
        if (Test-Path -LiteralPath $output) { throw 'Output already exists' }
        $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
            24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
            [System.Speech.AudioFormat.AudioChannel]::Mono)
        $synth.SetOutputToWaveFile($output, $format)
        $synth.Speak([string]$data.text)
        $synth.SetOutputToNull()
        $reply = @{version=1; ok=$true}
    } else { throw 'Unsupported speech operation' }
    $json = ConvertTo-Json -InputObject $reply -Depth 5 -Compress
    [IO.File]::WriteAllText((Join-Path $directory 'result.json'), $json,
                          (New-Object Text.UTF8Encoding($false)))
} catch {
    # Engine exceptions can include source text or private paths. Keep public
    # diagnostics generic; the parent reports a failed/cancelled local stage.
    [Console]::Error.WriteLine('The local SAPI5 operation failed.')
    exit 1
} finally {
    if ($null -ne $synth) { $synth.Dispose() }
}
