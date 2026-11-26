# Fixed packaged local SAPI5 worker. User text is JSON data, never executable code.
# No registry, execution-policy, voice-installation or network operations.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
Add-Type -AssemblyName System.Speech
$requestPath = Join-Path (Get-Location).Path 'request.json'
$request = [IO.File]::ReadAllText($requestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
if ($request.schema -ne 1) { throw 'Unsupported speech request schema.' }
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $installed = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled })
    if ($request.action -eq 'list') {
        $rows = @($installed | ForEach-Object {
            $info = $_.VoiceInfo
            $attributes = @{}
            foreach ($key in $info.AdditionalInfo.Keys) {
                $attributes[$key] = $info.AdditionalInfo[$key]
            }
            @{ id=$info.Id; name=$info.Name; language=$info.Culture.Name;
               description=$info.Description; attributes=$attributes }
        })
        @{ voices=$rows; bitness=([IntPtr]::Size * 8);
           runtime=([System.Speech.Synthesis.SpeechSynthesizer].Assembly.FullName) } |
            ConvertTo-Json -Depth 6 -Compress
    } elseif ($request.action -eq 'synthesize') {
        if ($request.text -isnot [string] -or $request.text.Length -eq 0 -or
            $request.text.Contains([char]0)) { throw 'Invalid plain speech text.' }
        $matches = @($installed | Where-Object { $_.VoiceInfo.Id -ceq $request.voice_id })
        if ($matches.Count -ne 1) { throw 'Selected voice is unavailable.' }
        $synth.SelectVoice($matches[0].VoiceInfo.Name)
        if ($synth.Voice.Id -cne $request.voice_id) { throw 'Ambiguous installed voice name.' }
        $rate = [int]$request.sapi_rate
        $volume = [int]$request.volume
        $sampleRate = [int]$request.sample_rate
        if ($rate -lt -10 -or $rate -gt 10 -or $volume -lt 0 -or $volume -gt 100 -or
            $sampleRate -notin @(8000,16000,22050,24000,32000,44100,48000)) {
            throw 'Unsupported speech settings.'
        }
        $synth.Rate = $rate
        $synth.Volume = $volume
        $output = Join-Path (Get-Location).Path 'render.wav'
        if ([IO.File]::Exists($output)) { throw 'Speech output exists.' }
        $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
            $sampleRate, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
            [System.Speech.AudioFormat.AudioChannel]::Mono)
        $synth.SetOutputToWaveFile($output, $format)
        # Speak(String) treats SSML-like markup as plain text; SpeakSsml is unused.
        $synth.Speak([string]$request.text)
        $synth.SetOutputToNull()
        @{ status='complete'; voice_id=$synth.Voice.Id } | ConvertTo-Json -Compress
    } else {
        throw 'Unsupported local speech operation.'
    }
} finally {
    $synth.Dispose()
}
