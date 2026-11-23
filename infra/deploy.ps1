<#
.SYNOPSIS
    Deploy the telemetry routing and alerting pipeline.
.DESCRIPTION
    1. Registers the resource providers the pipeline needs.
    2. Deploys main.bicep: IoT Hub (F1) with routing, Event Hubs, storage, the Flex
       Consumption function app, monitoring, and role assignments.
    3. Publishes the function code (remote build) and waits for the functions to index.
    4. Deploys events.bicep: IoT Hub connection events to the connectivity function.
    5. Writes .env for the devices and opsctl when it does not exist yet.
    Uses the signed-in Azure CLI identity as the operator.
.EXAMPLE
    ./infra/deploy.ps1 -AlertEmail you@example.com
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-iot-telemetry-dev',
    [string]$Location = 'centralindia',
    [string]$AlertEmail = ''
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

# az writes warnings and errors to stderr. Windows PowerShell 5.1 turns redirected stderr into
# error records, which 'Stop' would make fatal, so az runs under 'Continue' and the exit code
# decides success.
function Invoke-AzRaw {
    $ErrorActionPreference = 'Continue'
    & az @args
}

function Invoke-Az {
    $output = Invoke-AzRaw @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit $LASTEXITCODE)" }
    return $output
}

$python = Join-Path $repo '.venv/Scripts/python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

foreach ($namespace in 'Microsoft.Devices', 'Microsoft.EventHub', 'Microsoft.Storage', 'Microsoft.Web',
    'Microsoft.EventGrid', 'Microsoft.Insights', 'Microsoft.OperationalInsights') {
    if ((Invoke-AzRaw provider show --namespace $namespace --query registrationState -o tsv) -ne 'Registered') {
        Write-Host "Registering resource provider $namespace"
        Invoke-Az provider register --namespace $namespace --wait | Out-Null
    }
}

$operatorId = Invoke-Az ad signed-in-user show --query id -o tsv
Invoke-Az group create --name $ResourceGroup --location $Location `
    --tags project=azure-iot-telemetry-routing environment=dev -o none
Write-Host "Resource group $ResourceGroup ready in $Location"

$parameters = @("operatorPrincipalId=$operatorId")
if ($AlertEmail) { $parameters += "alertEmail=$AlertEmail" }

# A new hub identity can take a minute or two to be usable for its Event Hubs role, so the
# routing step may fail on the first attempt; the deployment is idempotent and is retried.
$outputs = $null
for ($attempt = 1; $attempt -le 3 -and -not $outputs; $attempt++) {
    $raw = Invoke-AzRaw deployment group create --resource-group $ResourceGroup `
        --name "telemetry-$(Get-Date -Format 'yyyyMMddHHmmss')" `
        --template-file (Join-Path $PSScriptRoot 'main.bicep') `
        --parameters $parameters --query properties.outputs -o json
    if ($LASTEXITCODE -eq 0) {
        $outputs = $raw | ConvertFrom-Json
    } elseif ($attempt -lt 3) {
        Write-Host "Infrastructure deployment attempt $attempt failed; retrying in 60 s" -ForegroundColor Yellow
        Start-Sleep -Seconds 60
    }
}
if (-not $outputs) { throw 'Infrastructure deployment failed three times; see the errors above.' }

$app = $outputs.functionAppName.value
$zip = Join-Path ([IO.Path]::GetTempPath()) "telemetry-functions-$([guid]::NewGuid().ToString('N')).zip"
try {
    & $python (Join-Path $PSScriptRoot 'package.py') (Join-Path $repo 'functions') $zip
    if ($LASTEXITCODE -ne 0) { throw 'Packaging the function app failed.' }
    Write-Host "Publishing function code to $app (remote build)"
    Invoke-Az functionapp deployment source config-zip --resource-group $ResourceGroup --name $app `
        --src $zip --build-remote true -o none
} finally {
    Remove-Item $zip -ErrorAction SilentlyContinue
}

$deadline = (Get-Date).AddMinutes(10)
$indexed = $false
while (-not $indexed -and (Get-Date) -lt $deadline) {
    $names = Invoke-AzRaw functionapp function list --resource-group $ResourceGroup --name $app --query '[].name' -o tsv 2>$null
    $indexed = [bool]($names | Where-Object { $_ -like '*/connectivity' })
    if (-not $indexed) { Start-Sleep -Seconds 15 }
}
if (-not $indexed) { throw "Functions did not appear on $app within 10 minutes; check its deployment logs." }
Write-Host 'Functions indexed: stream, connectivity, presence'

Invoke-Az deployment group create --resource-group $ResourceGroup `
    --name "telemetry-events-$(Get-Date -Format 'yyyyMMddHHmmss')" `
    --template-file (Join-Path $PSScriptRoot 'events.bicep') `
    --parameters "hubName=$($outputs.iotHubName.value)" "functionAppName=$app" -o none
Write-Host 'Event Grid subscription for device connection events created'

$values = [ordered]@{
    IOTHUB_HOSTNAME      = $outputs.iotHubHostName.value
    CERTS_DIR            = 'certs'
    STORAGE_ACCOUNT_NAME = $outputs.storageAccountName.value
}
$envFile = Join-Path $repo '.env'
if (Test-Path $envFile) {
    Write-Host "`n.env already exists; update it with:" -ForegroundColor Yellow
    $values.GetEnumerator() | ForEach-Object { Write-Host "$($_.Key)=$($_.Value)" }
} else {
    $values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" } | Set-Content -Path $envFile -Encoding ascii
    Write-Host "`nWrote $envFile" -ForegroundColor Green
}

Write-Host "`nDeployed hub $($outputs.iotHubName.value) and function app $app." -ForegroundColor Green
Write-Host 'Role assignments can take a few minutes before opsctl and the functions can use them.'
