# Oracle Cloud A1 Instance - Auto Retry Script
# Fill in your details below, then run in PowerShell:
# Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# .\oracle_retry.ps1

# ─── FILL THESE IN ────────────────────────────────────────────────────────────
$OCI_CONFIG    = "$env:USERPROFILE\.oci\config"   # path to your OCI config file
$COMPARTMENT   = "YOUR_COMPARTMENT_OCID"          # from Identity > Compartments
$SUBNET        = "YOUR_SUBNET_OCID"               # from Networking > VCN > Subnets
$SSH_KEY       = "YOUR_PUBLIC_KEY_CONTENTS"       # paste your public key string
$AD            = "YOUR-AD-1"                      # e.g. "LXpK:AP-MUMBAI-1-AD-1"
$SHAPE         = "VM.Standard.A1.Flex"
$OCPUS         = 4
$MEMORY        = 24
$IMAGE         = "YOUR_IMAGE_OCID"                # Ubuntu 24.04 OCID for your region
# ──────────────────────────────────────────────────────────────────────────────

Write-Host "Starting Oracle A1 auto-retry. Will attempt every 60s. Press Ctrl+C to stop." -ForegroundColor Cyan

$attempt = 0
while ($true) {
    $attempt++
    Write-Host "`n[$(Get-Date -Format 'HH:mm:ss')] Attempt $attempt..." -ForegroundColor Yellow

    $result = oci compute instance launch `
        --compartment-id $COMPARTMENT `
        --availability-domain $AD `
        --shape $SHAPE `
        --shape-config "{`"ocpus`":$OCPUS,`"memoryInGBs`":$MEMORY}" `
        --subnet-id $SUBNET `
        --image-id $IMAGE `
        --assign-public-ip true `
        --ssh-authorized-keys-file <(echo $SSH_KEY) `
        --display-name "tansen-bot" 2>&1

    if ($LASTEXITCODE -eq 0) {
        Write-Host "`n✅ SUCCESS! Instance created!" -ForegroundColor Green
        Write-Host $result
        break
    } elseif ($result -match "Out of capacity") {
        Write-Host "   Still out of capacity. Waiting 60s..." -ForegroundColor Red
        Start-Sleep -Seconds 60
    } else {
        Write-Host "   Unexpected error:" -ForegroundColor Red
        Write-Host $result
        break
    }
}
