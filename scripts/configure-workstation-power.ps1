[CmdletBinding()]
param(
    [ValidateSet('Status','Apply','Restore')][string]$Mode = 'Status',
    [string]$BackupPath = ''
)

$script:WpSettings = [ordered]@{
    LIDACTION = @('4f971e89-eebd-4455-a8de-9e59040e7347','5ca83367-6e45-459f-a27b-476b1d01c936')
    STANDBYIDLE = @('238c9fa8-0aad-41ed-83f4-97be242c8f20','29f6c1db-86da-48c5-9fdb-f2b67b1f44da')
    HIBERNATEIDLE = @('238c9fa8-0aad-41ed-83f4-97be242c8f20','9d7815a6-7ee4-497e-8888-515a05f02364')
}

function Invoke-WpPowerCfg([string[]]$Arguments) {
    try { $executable = (Get-Command powercfg.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source }
    catch { throw 'WP_POWERCFG_UNAVAILABLE' }
    $output = & $executable @Arguments 2>&1 | Out-String
    [pscustomobject]@{ExitCode=$LASTEXITCODE; Output=$output}
}

function Invoke-WpChecked([string[]]$Arguments) {
    $result = Invoke-WpPowerCfg -Arguments $Arguments
    if ($result.ExitCode -ne 0) { throw ('WP_NATIVE_EXIT_' + $result.ExitCode) }
    [string]$result.Output
}

function Get-WpActiveScheme {
    $output = Invoke-WpChecked -Arguments @('/getactivescheme')
    $guids = [regex]::Matches($output, '(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b')
    if ($guids.Count -ne 1) { throw 'WP_ACTIVE_SCHEME_INVALID' }
    $guids[0].Value.ToLowerInvariant()
}

function Get-WpSnapshot {
    $scheme = Get-WpActiveScheme
    $values = [ordered]@{}
    foreach ($alias in $script:WpSettings.Keys) {
        $ids = $script:WpSettings[$alias]
        $output = Invoke-WpChecked -Arguments @('/qh',$scheme,$ids[0],$ids[1])
        if ($output -notmatch [regex]::Escape($ids[1]) -or
                $output -notmatch ('(?m)^.*:\s*' + $alias + '\s*$')) { throw 'WP_SETTING_SCHEMA_INVALID' }
        # The final two nonempty lines are the current AC and DC indices in
        # powercfg's fixed order; preceding min/max values are not indices.
        $lines = @($output -split '\r?\n' | Where-Object { $_.Trim() })
        if ($lines.Count -lt 2) { throw 'WP_SETTING_SCHEMA_INVALID' }
        $indices = @($lines[-2],$lines[-1] | ForEach-Object {
            $matches = [regex]::Matches($_, '(?i)0x([0-9a-f]{1,8})\b')
            if ($matches.Count -ne 1) { throw 'WP_SETTING_SCHEMA_INVALID' }
            [Convert]::ToUInt32($matches[0].Groups[1].Value,16)
        })
        $values[$alias] = [ordered]@{AC=$indices[0]; DC=$indices[1]}
    }
    if ((Get-WpActiveScheme) -ne $scheme) { throw 'WP_ACTIVE_SCHEME_CHANGED' }
    [ordered]@{SchemeGuid=$scheme; Settings=$values}
}

function Get-WpHash($Value) {
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 8 -Compress))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Test-WpIgnoredPath([string]$Root,[string]$Path) {
    try { $executable = (Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source }
    catch { throw 'WP_GIT_UNAVAILABLE' }
    $null = & $executable -C $Root check-ignore --quiet --no-index -- $Path 2>&1
    $LASTEXITCODE -eq 0
}

function Resolve-WpDataPath([string]$Root,[string]$Path) {
    $data = [IO.Path]::GetFullPath((Join-Path $Root 'data'))
    $full = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($Path)) {$Path} else {Join-Path $Root $Path}))
    if (-not $full.StartsWith($data + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'WP_BACKUP_OUTSIDE_DATA'
    }
    $cursor = $full
    while ($cursor -and $cursor.Length -ge $data.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'WP_BACKUP_REPARSE_POINT'
            }
        }
        if ($cursor -eq $data) { break }
        $cursor = Split-Path -Parent $cursor
    }
    if (-not (Test-WpIgnoredPath -Root $Root -Path $full)) { throw 'WP_BACKUP_NOT_IGNORED' }
    $full
}

function Write-WpNewJson([string]$Path,$Value) {
    $null = [IO.Directory]::CreateDirectory((Split-Path -Parent $Path))
    $stream = [IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 8))
        $stream.Write($bytes,0,$bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
}

function Assert-WpSnapshot($Actual,$Expected) {
    if ((Get-WpHash $Actual) -ne (Get-WpHash $Expected)) { throw 'WP_POWER_STATE_CHANGED' }
}

function Assert-WpBackup($Backup) {
    if ($Backup.Format -ne 'workstation-power-ac-v1' -or
            $Backup.Before.SchemeGuid -notmatch '^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$' -or
            $Backup.BeforeHash -ne (Get-WpHash $Backup.Before) -or
            @($Backup.Before.Settings.PSObject.Properties).Count -ne 3) { throw 'WP_BACKUP_INVALID' }
    foreach ($alias in $script:WpSettings.Keys) {
        $value = $Backup.Before.Settings.$alias
        foreach ($kind in @('AC','DC')) {
            $index = $value.$kind
            if (($index -isnot [int] -and $index -isnot [long] -and $index -isnot [uint32]) -or
                    $index -lt 0 -or $index -gt [uint32]::MaxValue) { throw 'WP_BACKUP_INVALID' }
        }
    }
}

function Invoke-WorkstationPower([string]$Mode,[string]$BackupPath='',
        [string]$Root=(Split-Path -Parent $PSScriptRoot)) {
    $before = Get-WpSnapshot
    if ($Mode -eq 'Status') { return [ordered]@{Mode='Status'; State='read_only'; Current=$before} }
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [guid]::NewGuid().ToString('N')
    if ($Mode -eq 'Apply') {
        if ($BackupPath) { throw 'WP_APPLY_REQUIRES_FRESH_BACKUP' }
        $path = Resolve-WpDataPath -Root $Root -Path ('data/workstation-power/' + $stamp + '.local.json')
        $backup = [ordered]@{Format='workstation-power-ac-v1'; CapturedUtc=[DateTime]::UtcNow.ToString('o');
            Before=$before; BeforeHash=(Get-WpHash $before)}
        Write-WpNewJson -Path $path -Value $backup
        $desired = @{LIDACTION=0; STANDBYIDLE=0; HIBERNATEIDLE=0}
    } elseif ($Mode -eq 'Restore') {
        if (-not $BackupPath) { throw 'WP_RESTORE_BACKUP_REQUIRED' }
        $path = Resolve-WpDataPath -Root $Root -Path $BackupPath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -gt 65536) {
            throw 'WP_BACKUP_INVALID'
        }
        $backup = Get-Content -LiteralPath $path -Raw -Encoding utf8 | ConvertFrom-Json
        Assert-WpBackup $backup
        if ($before.SchemeGuid -ne $backup.Before.SchemeGuid) { throw 'WP_ACTIVE_SCHEME_CHANGED' }
        $desired = @{}
        foreach ($alias in $script:WpSettings.Keys) {
            if ($before.Settings[$alias].DC -ne $backup.Before.Settings.$alias.DC) { throw 'WP_DC_CHANGED' }
            $desired[$alias] = $backup.Before.Settings.$alias.AC
        }
    } else { throw 'WP_MODE_INVALID' }
    $relative = $path.Substring([IO.Path]::GetFullPath($Root).Length + 1).Replace('\','/')
    $intentPath = Resolve-WpDataPath -Root $Root -Path ($path + '.' + $Mode.ToLowerInvariant() + '-' + $stamp + '.intent.json')
    $resultPath = Resolve-WpDataPath -Root $Root -Path ($intentPath + '.result.json')
    Write-WpNewJson -Path $intentPath -Value ([ordered]@{Mode=$Mode; State='intent'; Backup=$relative;
        BackupHash=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant(); Before=$before; DesiredAC=$desired})
    $expected = $before
    try {
        foreach ($alias in $script:WpSettings.Keys) {
            Assert-WpSnapshot -Actual (Get-WpSnapshot) -Expected $expected
            $ids = $script:WpSettings[$alias]
            $null = Invoke-WpChecked -Arguments @('/setacvalueindex',$before.SchemeGuid,$ids[0],$ids[1],[string]$desired[$alias])
            $expected.Settings[$alias].AC = [uint32]$desired[$alias]
            Assert-WpSnapshot -Actual (Get-WpSnapshot) -Expected $expected
        }
        # Reapply only the still-current scheme; never activate a different one.
        Assert-WpSnapshot -Actual (Get-WpSnapshot) -Expected $expected
        $null = Invoke-WpChecked -Arguments @('/setactive',$before.SchemeGuid)
        Assert-WpSnapshot -Actual (Get-WpSnapshot) -Expected $expected
        Write-WpNewJson -Path $resultPath -Value ([ordered]@{State='verified'; Mode=$Mode; Current=$expected})
        [ordered]@{Mode=$Mode; State='verified'; Backup=$relative; Current=$expected; DCUnchanged=$true}
    } catch {
        $safeError = if ($_.Exception.Message -match '^WP_[A-Z0-9_]+$') {$_.Exception.Message} else {'WP_OPERATION_FAILED'}
        try { Write-WpNewJson -Path $resultPath -Value @{State='failed_no_retry'; Mode=$Mode; Error=$safeError; Backup=$relative} } catch {}
        throw $safeError
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-WorkstationPower -Mode $Mode -BackupPath $BackupPath | ConvertTo-Json -Depth 8 }
    catch {
        $safeError = if ($_.Exception.Message -match '^WP_[A-Z0-9_]+$') {$_.Exception.Message} else {'WP_OPERATION_FAILED'}
        Write-Error $safeError
        exit 1
    }
}
