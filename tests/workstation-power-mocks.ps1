param([Parameter(Mandatory=$true)][string]$ProjectRoot,[Parameter(Mandatory=$true)][string]$ScriptPath)
$ErrorActionPreference = 'Stop'
. $ScriptPath
$script:ActualIgnoredRunner = ${function:Test-WpIgnoredPath}
$script:ActualNativeRunner = ${function:Invoke-WpPowerCfg}
$script:FakeScheme = '11111111-2222-3333-4444-555555555555'
$script:Passed = 0
function Assert-WpTest($Condition,[string]$Name) { if (-not $Condition) { throw ('TEST_FAILED_' + $Name) } }
function Start-WpCase([string]$Name) {
    $script:CaseRoot = Join-Path $ProjectRoot $Name
    $null = [IO.Directory]::CreateDirectory((Join-Path $script:CaseRoot 'data'))
    $script:State = [ordered]@{SchemeGuid=$script:FakeScheme; Settings=[ordered]@{
        LIDACTION=[ordered]@{AC=1; DC=1}; STANDBYIDLE=[ordered]@{AC=0; DC=1200}; HIBERNATEIDLE=[ordered]@{AC=60; DC=60}}}
    $script:Calls = [Collections.Generic.List[object]]::new()
    $script:Writes=0; $script:FailWrite=0; $script:DriftDC=$false; $script:DriftScheme=$false; $script:Ignored=$true
}
function Test-WpIgnoredPath([string]$Root,[string]$Path) { $script:Ignored }
function Invoke-WpPowerCfg([string[]]$Arguments) {
    $script:Calls.Add(@($Arguments))
    if ($Arguments[0] -eq '/getactivescheme') { return @{ExitCode=0; Output=('Active: '+$script:State.SchemeGuid)} }
    if ($Arguments[0] -eq '/qh') {
        $alias = @($script:WpSettings.Keys | Where-Object {$script:WpSettings[$_][1] -eq $Arguments[3]})[0]
        Assert-WpTest ($null -ne $alias) 'unknown_setting'
        $v=$script:State.Settings[$alias]
        return @{ExitCode=0; Output=("Setting: $($Arguments[3])`nAlias: $alias`nMin: 0x00000000`nMax: 0xffffffff`nAC: 0x$('{0:x8}' -f $v.AC)`nDC: 0x$('{0:x8}' -f $v.DC)`n")}
    }
    if ($Arguments[0] -eq '/setacvalueindex') {
        $script:Writes++
        Assert-WpTest ($Arguments.Count -eq 5 -and $Arguments[1] -eq $script:FakeScheme) 'exact_scheme_and_array'
        if ($script:Writes -eq $script:FailWrite) { return @{ExitCode=5; Output='native failure'} }
        $alias = @($script:WpSettings.Keys | Where-Object {$script:WpSettings[$_][1] -eq $Arguments[3]})[0]
        Assert-WpTest ($Arguments[2] -eq $script:WpSettings[$alias][0]) 'exact_subgroup'
        $script:State.Settings[$alias].AC=[uint32]$Arguments[4]
        if ($script:DriftDC) { $script:State.Settings.LIDACTION.DC=2 }
        if ($script:DriftScheme) { $script:State.SchemeGuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' }
        return @{ExitCode=0; Output=''}
    }
    if ($Arguments[0] -eq '/setactive') {
        Assert-WpTest ($Arguments[1] -eq $script:State.SchemeGuid) 'never_activate_other_scheme'
        return @{ExitCode=0; Output=''}
    }
    throw 'TEST_UNEXPECTED_NATIVE_COMMAND'
}
function Expect-WpError([scriptblock]$Operation,[string]$Code) {
    try { $null = & $Operation; throw 'TEST_EXPECTED_FAILURE' }
    catch { Assert-WpTest ($_.Exception.Message -match $Code) 'safe_failure_code' }
}
function Get-WpCaseBackup { @(Get-ChildItem -LiteralPath (Join-Path $script:CaseRoot 'data/workstation-power') -Filter '*.local.json')[0].FullName }

Start-WpCase 'status'
$value=Invoke-WorkstationPower -Mode Status -Root $script:CaseRoot
Assert-WpTest ($value.State -eq 'read_only' -and $script:Writes -eq 0) 'status_read_only'
$script:Passed++

Start-WpCase 'apply_restore'
$value=Invoke-WorkstationPower -Mode Apply -Root $script:CaseRoot
$backup=Get-WpCaseBackup
$backupHash=(Get-FileHash -LiteralPath $backup).Hash
$saved=Get-Content -LiteralPath $backup -Raw | ConvertFrom-Json
Assert-WpTest ($value.State -eq 'verified' -and $script:Writes -eq 3 -and $value.Backup.StartsWith('data/')) 'apply_verified'
Assert-WpTest ($saved.Before.Settings.LIDACTION.AC -eq 1 -and $saved.Before.Settings.HIBERNATEIDLE.AC -eq 60) 'backup_original_ac'
Assert-WpTest ($saved.Before.Settings.STANDBYIDLE.DC -eq 1200 -and $saved.Before.Settings.HIBERNATEIDLE.DC -eq 60) 'backup_original_dc'
foreach ($alias in $script:WpSettings.Keys) { Assert-WpTest ($script:State.Settings[$alias].AC -eq 0) 'ac_zero' }
$value=Invoke-WorkstationPower -Mode Restore -BackupPath $backup -Root $script:CaseRoot
Assert-WpTest ($value.State -eq 'verified' -and $script:Writes -eq 6 -and $script:State.Settings.LIDACTION.AC -eq 1 -and $script:State.Settings.HIBERNATEIDLE.AC -eq 60) 'restore_original_ac'
Assert-WpTest ((Get-FileHash -LiteralPath $backup).Hash -eq $backupHash) 'immutable_backup'
Assert-WpTest (@($script:Calls | Where-Object {$_[0] -eq '/setdcvalueindex'}).Count -eq 0) 'never_write_dc'
$script:Passed+=2

Start-WpCase 'partial_native_failure'
$script:FailWrite=2
Expect-WpError { Invoke-WorkstationPower -Mode Apply -Root $script:CaseRoot } 'WP_NATIVE_EXIT_5'
$backup=Get-WpCaseBackup
Assert-WpTest ($script:Writes -eq 2 -and (Test-Path -LiteralPath $backup)) 'stop_native_error_keep_backup'
Assert-WpTest (@($script:Calls | Where-Object {$_[0] -eq '/setactive'}).Count -eq 0) 'no_reapply_after_error'
Assert-WpTest (@(Get-ChildItem -LiteralPath (Split-Path -Parent $backup) -Filter '*.result.json').Count -eq 1) 'failure_receipt'
$script:FailWrite=0
$null=Invoke-WorkstationPower -Mode Restore -BackupPath $backup -Root $script:CaseRoot
Assert-WpTest ($script:State.Settings.LIDACTION.AC -eq 1) 'explicit_partial_restore'
$script:Passed++

Start-WpCase 'dc_drift'
$script:DriftDC=$true
Expect-WpError { Invoke-WorkstationPower -Mode Apply -Root $script:CaseRoot } 'WP_POWER_STATE_CHANGED'
Assert-WpTest ($script:Writes -eq 1 -and $script:State.Settings.LIDACTION.DC -eq 2) 'stop_without_repairing_dc'
$backup=Get-WpCaseBackup
Expect-WpError { Invoke-WorkstationPower -Mode Restore -BackupPath $backup -Root $script:CaseRoot } 'WP_DC_CHANGED'
Assert-WpTest ($script:Writes -eq 1) 'restore_refuses_dc_drift'
$script:Passed++

Start-WpCase 'scheme_drift'
$script:DriftScheme=$true
Expect-WpError { Invoke-WorkstationPower -Mode Apply -Root $script:CaseRoot } 'WP_POWER_STATE_CHANGED'
Assert-WpTest ($script:Writes -eq 1 -and @($script:Calls | Where-Object {$_[0] -eq '/setactive'}).Count -eq 0) 'stop_scheme_drift'
$backup=Get-WpCaseBackup
Expect-WpError { Invoke-WorkstationPower -Mode Restore -BackupPath $backup -Root $script:CaseRoot } 'WP_ACTIVE_SCHEME_CHANGED'
$script:Passed++

Start-WpCase 'outside_data'
Expect-WpError { Invoke-WorkstationPower -Mode Restore -BackupPath (Join-Path $script:CaseRoot 'outside.json') -Root $script:CaseRoot } 'WP_BACKUP_OUTSIDE_DATA'
Assert-WpTest ($script:Writes -eq 0) 'outside_path_no_mutation'
$script:Passed++

Start-WpCase 'not_ignored'
$script:Ignored=$false
Expect-WpError { Invoke-WorkstationPower -Mode Apply -Root $script:CaseRoot } 'WP_BACKUP_NOT_IGNORED'
Assert-WpTest ($script:Writes -eq 0) 'nonignored_path_no_mutation'
$script:Passed++

Start-WpCase 'no_overwrite'
$path=Join-Path $script:CaseRoot 'data/existing.json'
Write-WpNewJson -Path $path -Value @{original=$true}
$hash=(Get-FileHash -LiteralPath $path).Hash
try { Write-WpNewJson -Path $path -Value @{overwritten=$true}; throw 'TEST_OVERWRITE_WAS_ALLOWED' }
catch { Assert-WpTest ($_.Exception.Message -notmatch 'TEST_OVERWRITE_WAS_ALLOWED') 'create_new_only' }
Assert-WpTest ((Get-FileHash -LiteralPath $path).Hash -eq $hash) 'original_file_intact'
$script:Passed++

# Exercise the real executable-selection wrappers, rather than the native
# runner mocks above. Multiple applications must never become one command name.
Start-WpCase 'duplicate_applications'
$script:FakeGitArguments = Join-Path $script:CaseRoot 'git-arguments.json'
$script:FakeGitFirst = Join-Path $script:CaseRoot 'git first.ps1'
$script:FakePowerFirst = Join-Path $script:CaseRoot 'power first.ps1'
$script:FakeSecond = Join-Path $script:CaseRoot 'second must not run.ps1'
$gitProgram = @'
$args | ConvertTo-Json -Compress | Set-Content -LiteralPath '__ARGUMENTS__' -Encoding utf8
$global:LASTEXITCODE = 0
'@
[IO.File]::WriteAllText($script:FakeGitFirst,$gitProgram.Replace('__ARGUMENTS__',$script:FakeGitArguments.Replace("'","''")))
[IO.File]::WriteAllText($script:FakePowerFirst,"Write-Output 'Active: 11111111-2222-3333-4444-555555555555'`n`$global:LASTEXITCODE = 0")
[IO.File]::WriteAllText($script:FakeSecond,"throw 'TEST_SECOND_APPLICATION_EXECUTED'")
function Get-Command {
    [CmdletBinding()]param([string]$Name,[object]$CommandType)
    $first = if ($Name -eq 'git') {$script:FakeGitFirst} elseif ($Name -eq 'powercfg.exe') {$script:FakePowerFirst} else {throw 'TEST_UNEXPECTED_LOOKUP'}
    [pscustomobject]@{Source=$first}
    [pscustomobject]@{Source=$script:FakeSecond}
}
$ignored = & $script:ActualIgnoredRunner -Root $script:CaseRoot -Path (Join-Path $script:CaseRoot 'data/probe.local.json')
Assert-WpTest $ignored 'duplicate_git_select_first'
$received = @(Get-Content -LiteralPath $script:FakeGitArguments -Raw | ConvertFrom-Json)
# A PowerShell script stub consumes the -- separator; the real native git
# receives it. Root and backup paths must remain individual arguments in both.
Assert-WpTest ($received.Count -in @(6,7) -and $received[1] -eq $script:CaseRoot -and
    $received[2] -eq 'check-ignore' -and $received[-1] -eq (Join-Path $script:CaseRoot 'data/probe.local.json')) 'git_argument_array_preserved'
$native = & $script:ActualNativeRunner -Arguments @('/getactivescheme')
Assert-WpTest ($native.ExitCode -eq 0 -and $native.Output -match '11111111-2222-3333-4444-555555555555') 'duplicate_powercfg_select_first'
$script:Passed++

@{passed=$script:Passed; real_power_mutations=0; native_runner='mock'} | ConvertTo-Json -Compress
