<#
PayPal refund agent: the live consent evaluation, for your own PowerShell window. One command:

    .\scripts\live_model_eval.ps1                      # Haiku 5.5, hard cap $1.00 for all four sets together
    .\scripts\live_model_eval.ps1 -Model claude-sonnet-5-5 -MaxSpend 3.00     # a bigger model needs a bigger cap (see the dry run)

What it does, in order:
  1. DRY RUN (no key, nothing sent): prints, per set, the number of model calls and the worst-case cost at the chosen model's price,
     and the total against the cap.
  2. Asks for a y/n before any spending. Anything but y stops here and nothing is spent.
  3. Takes the key: uses ANTHROPIC_API_KEY if it is already set in this window, otherwise asks for it with a hidden prompt. The key lives
     in this process only, is never printed, never written to a file, and is removed again at the end if this script set it.
  4. LIVE RUN of dhanush_m1, codex_redteam, codex_pass2 and dev in that order under ONE cap, through a shared ledger
     (eval\spend_ledger_live.json, holds dollars and counts, no key). Every call is reserved against the cap before it is sent. When the
     next case could not fit, the run stops cleanly, the result is labelled PARTIAL, and a case that was not run is not counted.
  5. Prints the SUMMARY. Keep the console output (from the DRY RUN line down); it contains no key.

Exit code 0 = every set complete, 4 = PARTIAL or NOT RUN (the cap or a rejected key stopped it), 2 = could not start.
Replies the model gave are saved to eval\consent_recordings_live.json for replay with --consent recorded: `entries` holds the accepted consent
replies; `rejected_readings` and `extractor_exchanges` (same file) keep the rejected consent replies and every live extractor exchange with its
verdict, so a failure can be explained without paying for another run.
Rerunning after a stop or a crash: run the same command again WITHOUT -FreshLedger. The ledger keeps the spend already made, so the cap
covers the first run and the rerun together; every set is run again from its first case (no case is skipped or resumed, which keeps each
set's result whole and simple). The recordings file is rewritten by the rerun. The recordings are saved only after the results are
printed, atomically; if the save fails the console says COULD NOT SAVE and the exit code is 5 (results are still complete).
Run from the repository root. PayPal itself stays a fixture: no PayPal call, no real money.
#>
param(
    [string]$Model = 'claude-haiku-5-5',
    [decimal]$MaxSpend = 1.00,
    [switch]$FreshLedger
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$env:PYTHONUTF8 = '1'
$venvPy = Join-Path $repo '.venv\Scripts\python.exe'
$py = if (Test-Path $venvPy) { $venvPy } else { 'python' }

$sets = 'dhanush_m1,codex_redteam,codex_pass2,dev'
$ledger = 'eval/spend_ledger_live.json'
$saved = 'eval/consent_recordings_live.json'
if (-not $env:APP_MODEL_ID) { $env:APP_MODEL_ID = $Model }
$env:APP_MODEL_PROVIDER = 'anthropic'
if ($FreshLedger -and (Test-Path $ledger)) { Remove-Item $ledger }
$cap = $MaxSpend.ToString([System.Globalization.CultureInfo]::InvariantCulture)

# Run Python and return its exit code. Under Windows PowerShell 5.1 a native command's stderr line becomes an error record, which
# with $ErrorActionPreference = 'Stop' would end the script (for instance at the cap notice), so stderr is folded into plain text.
function Invoke-Py([string[]]$PyArgs) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $py @PyArgs 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
        } | Out-Host
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
}

$common = @('-m', 'app.evaluation', 'repro', '--set', $sets, '--consent', 'live', '--max-spend', $cap, '--ledger', $ledger)

# 1. DRY RUN: no key is read, nothing is sent.
$rc = Invoke-Py ($common + @('--dry-run'))
if ($rc -ne 0) { Write-Host "The dry run could not complete (exit $rc). Nothing was spent."; exit 2 }
if (Test-Path $ledger) { Write-Host "A ledger from an earlier run exists ($ledger): its spend counts against this cap. Use -FreshLedger to start from zero." }

# 2. Ask.
$answer = Read-Host "Continue? This will make live model calls on your key, up to the cap above (y/N)"
if ($answer -notmatch '^(y|yes)$') { Write-Host 'Stopped. Nothing was spent.'; exit 0 }

# 3. The key: already in this window, or a hidden prompt. Never printed, never stored.
$setKeyHere = $false
if (-not $env:ANTHROPIC_API_KEY) {
    $secure = Read-Host 'Anthropic API key (input is hidden)' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $setKeyHere = $true
}
if (-not $env:ANTHROPIC_API_KEY) { Write-Host 'No key given. Nothing was spent.'; exit 2 }

# LIVE RUN: the four sets in order under one cap.
try {
    $rc = Invoke-Py ($common + @('--save-consent', $saved))
} finally {
    if ($setKeyHere) { Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue }
}
switch ($rc) {
    0 { Write-Host "`nAll four sets ran to the end under the cap." }
    4 { Write-Host "`nPARTIAL: the cap, a rejected key or repeated failures stopped the run. Only completed cases are counted; the summary above says which." }
    default { Write-Host "`nThe run ended with exit code $rc." }
}
Write-Host 'Keep the console output (from the DRY RUN line down). It contains no key.'
exit $rc
