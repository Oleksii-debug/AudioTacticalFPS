param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$requestText = [System.IO.File]::ReadAllText($Request, [System.Text.Encoding]::UTF8)
$requestData = $requestText | ConvertFrom-Json
if ($requestData.version -ne 1) { throw 'Unsupported speech request version.' }
$speaker = New-Object -ComObject SAPI.SpVoice
$tokens = $speaker.GetVoices()
if ($requestData.operation -eq 'voices') {
    $voices = @()
    for ($index = 0; $index -lt $tokens.Count; $index++) {
        $token = $tokens.Item($index)
        $language = ''
        $vendor = ''
        try { $language = $token.GetAttribute('Language') } catch {}
        try { $vendor = $token.GetAttribute('Vendor') } catch {}
        $voices += @{token=$token.Id; name=$token.GetDescription(); language=$language; vendor=$vendor}
    }
    @{version=1; voices=$voices} | ConvertTo-Json -Depth 5 -Compress
} elseif ($requestData.operation -eq 'render') {
    $selected = $null
    for ($index = 0; $index -lt $tokens.Count; $index++) {
        $candidate = $tokens.Item($index)
        if ($candidate.Id -ceq $requestData.token) { $selected = $candidate; break }
    }
    if ($null -eq $selected) { throw 'The selected installed voice is no longer available.' }
    $speaker.Voice = $selected
    $speaker.Rate = [int]$requestData.rate
    $speaker.Volume = [int]$requestData.volume
    $stream = New-Object -ComObject SAPI.SpFileStream
    try {
        $stream.Open([string]$requestData.output, 3, $false)
        $speaker.AudioOutputStream = $stream
        # SVSFIsNotXML=16: source text cannot become SSML, file inclusion or commands.
        $flags = 16
        if ($requestData.punctuation -eq $true) { $flags = $flags -bor 64 }
        $null = $speaker.Speak([string]$requestData.text, $flags)
    } finally {
        $stream.Close()
    }
    @{version=1; status='rendered'} | ConvertTo-Json -Compress
} else {
    throw 'Unsupported speech operation.'
}
