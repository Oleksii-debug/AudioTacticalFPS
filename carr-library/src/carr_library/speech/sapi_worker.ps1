param(
    [Parameter(Mandatory=$true)][string]$Request,
    [string]$PhaseTrace = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$workerClock = [System.Diagnostics.Stopwatch]::StartNew()

function Write-Phase([string]$Name) {
    # Only fixed phase names and numeric timings enter this private diagnostic.
    # Closing each append preserves the last reached phase if the host times out.
    if (-not [string]::IsNullOrEmpty($PhaseTrace)) {
        try {
            $line = $Name + "`t" + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + "`t" + $workerClock.ElapsedMilliseconds + "`n"
            [System.IO.File]::AppendAllText($PhaseTrace, $line, [System.Text.Encoding]::ASCII)
        } catch {}
    }
}

Write-Phase 'script-entered'
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $requestFile = [System.IO.FileInfo]::new($Request)
    if ($requestFile.Length -lt 1 -or $requestFile.Length -gt 8000000) {
        throw 'Invalid speech request size.'
    }
    $requestText = [System.IO.File]::ReadAllText($Request, [System.Text.Encoding]::UTF8)
    Write-Phase 'request-read'

    # Fixed framework calls avoid loading PowerShell's Utility module for object
    # construction and JSON on every cold offline host. OS execution policy,
    # signature verification and CRL settings remain untouched.
    Write-Phase 'json-runtime-loading'
    $null = [System.Reflection.Assembly]::Load('System.Web.Extensions, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
    # NO type resolver or custom converters: request data cannot choose a .NET
    # class, a COM program ID or executable code.
    $serializer = [System.Web.Script.Serialization.JavaScriptSerializer]::new()
    $serializer.MaxJsonLength = 8000000
    $serializer.RecursionLimit = 4
    Write-Phase 'json-runtime-ready'
    $requestData = $serializer.DeserializeObject($requestText)
    if ($requestData -isnot [System.Collections.IDictionary] -or
        $requestData['version'] -isnot [int] -or $requestData['version'] -ne 1 -or
        $requestData['operation'] -isnot [string]) {
        throw 'Invalid speech request contract.'
    }
    if ($requestData['operation'] -ceq 'voices') {
        $allowed = @('version', 'operation')
    } elseif ($requestData['operation'] -ceq 'render') {
        $allowed = @('version', 'operation', 'token', 'text', 'output', 'rate', 'volume', 'punctuation')
        foreach ($name in @('token', 'text', 'output')) {
            if ($requestData[$name] -isnot [string] -or
                [string]::IsNullOrWhiteSpace($requestData[$name]) -or
                $requestData[$name].IndexOf([char]0) -ge 0) {
                throw 'Invalid speech request text field.'
            }
        }
        if ($requestData['token'].Length -gt 4096 -or $requestData['text'].Length -gt 4000000 -or
            $requestData['output'].Length -gt 32767 -or
            $requestData['rate'] -isnot [int] -or $requestData['rate'] -lt -10 -or $requestData['rate'] -gt 10 -or
            $requestData['volume'] -isnot [int] -or $requestData['volume'] -lt 0 -or $requestData['volume'] -gt 100 -or
            $requestData['punctuation'] -isnot [bool]) {
            throw 'Invalid speech request settings.'
        }
        # Python supplies a new private local staging file, never a user command.
        if ($requestData['output'] -notmatch '^[A-Za-z]:[\\/]' -or
            [System.IO.File]::Exists($requestData['output']) -or
            [System.IO.Directory]::Exists($requestData['output'])) {
            throw 'Speech requires a new local output file.'
        }
    } else {
        throw 'Unsupported speech operation.'
    }
    if ($requestData.Count -ne $allowed.Count) { throw 'Unexpected speech request fields.' }
    foreach ($name in $requestData.Keys) {
        if ($allowed -cnotcontains $name) { throw 'Unexpected speech request field.' }
    }
    Write-Phase 'request-validated'

    Write-Phase 'sapi-activating'
    $speaker = [System.Activator]::CreateInstance([type]::GetTypeFromProgID('SAPI.SpVoice', $true))
    Write-Phase 'sapi-ready'
    Write-Phase 'voices-enumerating'
    $tokens = $speaker.GetVoices()
    if ($tokens.Count -gt 10000) { throw 'Too many installed speech voices.' }
    Write-Phase 'voices-ready'
    if ($requestData['operation'] -ceq 'voices') {
        $voices = [System.Collections.Generic.List[object]]::new()
        for ($index = 0; $index -lt $tokens.Count; $index++) {
            $token = $tokens.Item($index)
            $language = ''
            $vendor = ''
            try { $language = $token.GetAttribute('Language') } catch {}
            try { $vendor = $token.GetAttribute('Vendor') } catch {}
            $voices.Add(@{token=$token.Id; name=$token.GetDescription(); language=$language; vendor=$vendor})
        }
        $response = @{version=1; voices=$voices}
    } else {
        $selected = $null
        for ($index = 0; $index -lt $tokens.Count; $index++) {
            $candidate = $tokens.Item($index)
            if ($candidate.Id -ceq $requestData['token']) { $selected = $candidate; break }
        }
        if ($null -eq $selected) { throw 'The selected installed voice is no longer available.' }
        $speaker.Voice = $selected
        $speaker.Rate = $requestData['rate']
        $speaker.Volume = $requestData['volume']
        Write-Phase 'voice-selected'
        Write-Phase 'stream-opening'
        $stream = [System.Activator]::CreateInstance([type]::GetTypeFromProgID('SAPI.SpFileStream', $true))
        try {
            $stream.Open($requestData['output'], 3, $false)
            $speaker.AudioOutputStream = $stream
            # SVSFIsNotXML=16 keeps XML-looking input, paths, quotes and PowerShell
            # expressions as original plain text. The filename flag is never set.
            $flags = 16
            if ($requestData['punctuation']) { $flags = $flags -bor 64 }
            Write-Phase 'speech-started'
            $null = $speaker.Speak($requestData['text'], $flags)
            Write-Phase 'speech-complete'
        } finally {
            $stream.Close()
        }
        Write-Phase 'stream-closed'
        $response = @{version=1; status='rendered'}
    }
    Write-Phase 'response-serializing'
    $reply = $serializer.Serialize($response)
    Write-Phase 'response-ready'
    [Console]::WriteLine($reply)
    Write-Phase 'complete'
} catch {
    Write-Phase 'failed'
    # Parser/provider diagnostics can contain private request text or local paths.
    [Console]::Error.WriteLine('The local SAPI worker failed.')
    exit 1
}
