# Start the JobRadar dashboard on a fixed port if it isn't already listening.
# Run by Claude Code hooks (SessionStart + UserPromptSubmit); silent when already up.
param([int]$Port = 8765)

$root = Split-Path -Parent $PSScriptRoot
$client = New-Object System.Net.Sockets.TcpClient
try {
    $ar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
    $up = $ar.AsyncWaitHandle.WaitOne(300) -and $client.Connected
} catch { $up = $false } finally { $client.Close() }

if (-not $up) {
    Start-Process -FilePath "uv" -ArgumentList "run", "--no-sync", "python", "-m", "jobradar", "dashboard", "--port", "$Port", "--no-open" `
        -WorkingDirectory $root -WindowStyle Hidden
    Write-Output "JobRadar dashboard started: http://127.0.0.1:$Port/"
}
