# JobRadar daily run: fetch, score, draft, export shortlist, then send what is allowed.
# Scheduled by scripts/install_schedule.ps1. Logs to out/logs/YYYY-MM-DD.log.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force "out/logs" | Out-Null
$log = "out/logs/$(Get-Date -Format yyyy-MM-dd).log"

function Step($name, [scriptblock]$cmd) {
    "`n=== $name $(Get-Date -Format HH:mm:ss) ===" | Out-File $log -Append -Encoding utf8
    & $cmd *>&1 | Out-File $log -Append -Encoding utf8
}

Step "run" { uv run jobradar run }
Step "shortlist" { uv run jobradar top --xlsx out/shortlist.xlsx }
# The sender needs the Gmail connector and the local WhatsApp MCP, so it runs here, not in CI.
Step "send" { claude -p "/send-outreach" --permission-mode dontAsk }
