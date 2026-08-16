# Persistent OCR worker built on the OCR engine already present in Windows.
#
# Reads one image path per line on stdin, writes one JSON result per line on
# stdout. The engine is created once, so each recognition costs a few
# milliseconds rather than paying process startup every time.
#
#   echo C:\path\to\card.png | powershell -File ocr_worker.ps1
#
# Send QUIT to exit.

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($task, $resultType) {
    $m = $asTaskGeneric.MakeGenericMethod($resultType)
    $net = $m.Invoke($null, @($task))
    $net.Wait(-1) | Out-Null
    $net.Result
}

[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Media, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { Write-Output '{"ok":false,"error":"no OCR engine available"}'; exit 1 }

# One engine per language, created on demand and reused. A card printed in
# Chinese needs the Chinese recogniser; the English one returns nothing useful.
$engines = @{ $engine.RecognizerLanguage.LanguageTag = $engine }

function Get-Engine([string]$tag) {
    if ([string]::IsNullOrWhiteSpace($tag)) { return $engine }
    if ($engines.ContainsKey($tag)) { return $engines[$tag] }
    try {
        $lang = New-Object Windows.Globalization.Language $tag
        $e = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    } catch { $e = $null }
    if ($null -eq $e) { return $null }
    $engines[$tag] = $e
    return $e
}

$available = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
               ForEach-Object { $_.LanguageTag }) -join ','
Write-Output ('{"ok":true,"ready":true,"lang":"' +
              $engine.RecognizerLanguage.LanguageTag +
              '","available":"' + $available + '"}')

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '' ) { continue }
    if ($line -eq 'QUIT') { break }

    # "<path>" or "<path>\t<language tag>"
    $parts = $line -split "`t", 2
    $path = $parts[0]
    $reqLang = if ($parts.Count -gt 1) { $parts[1].Trim() } else { '' }
    $useEngine = Get-Engine $reqLang
    if ($null -eq $useEngine) {
        Write-Output ([ordered]@{ ok = $false
            error = "no recogniser for '$reqLang'; have $available" } |
            ConvertTo-Json -Compress)
        continue
    }

    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($useEngine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $sw.Stop()

        $lines = @()
        foreach ($ln in $result.Lines) {
            $words = @($ln.Words)
            $x = ($words | ForEach-Object { $_.BoundingRect.X } | Measure-Object -Minimum).Minimum
            $y = ($words | ForEach-Object { $_.BoundingRect.Y } | Measure-Object -Minimum).Minimum
            $r = ($words | ForEach-Object { $_.BoundingRect.X + $_.BoundingRect.Width } | Measure-Object -Maximum).Maximum
            $b = ($words | ForEach-Object { $_.BoundingRect.Y + $_.BoundingRect.Height } | Measure-Object -Maximum).Maximum
            $lines += [ordered]@{
                text = $ln.Text
                x    = [int]$x
                y    = [int]$y
                w    = [int]($r - $x)
                h    = [int]($b - $y)
            }
        }

        $stream.Dispose()
        $payload = [ordered]@{
            ok    = $true
            ms    = [int]$sw.ElapsedMilliseconds
            text  = $result.Text
            lines = $lines
        }
        Write-Output ($payload | ConvertTo-Json -Compress -Depth 5)
    }
    catch {
        $err = ($_.Exception.Message -replace '[\r\n]+', ' ')
        Write-Output ([ordered]@{ ok = $false; error = $err } | ConvertTo-Json -Compress)
    }
}
