param([switch]$Deep)

$projectRoot = Split-Path -Parent $PSScriptRoot
$sshKey = Join-Path $projectRoot 'data/linux-vm_ed25519'
$knownHosts = Join-Path $projectRoot 'data/linux-vm-known-hosts'
if (-not (Test-Path -LiteralPath $sshKey) -or -not (Test-Path -LiteralPath $knownHosts)) {
    throw 'Private SSH key or pinned known-hosts file is missing from ignored data/.'
}

$remoteCommand = 'python3 /opt/max-postiz/deployment/linux/check-stack.py'
if ($Deep) { $remoteCommand += ' --deep' }
Push-Location $projectRoot
try {
    & ssh -i data/linux-vm_ed25519 -o ConnectTimeout=8 -o StrictHostKeyChecking=yes `
        -o UserKnownHostsFile=data/linux-vm-known-hosts editorial@10.20.30.45 $remoteCommand
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
