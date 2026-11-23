<#
.SYNOPSIS
    Delete every Azure resource for azure-iot-telemetry-routing.
.DESCRIPTION
    Removes the resource group: IoT Hub, Event Hubs, storage (archive and alert tables),
    the function app, Event Grid, and monitoring. Local device certificates are kept.
.EXAMPLE
    ./infra/teardown.ps1 -ResourceGroup rg-iot-telemetry-dev
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-iot-telemetry-dev',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

if ((az group exists --name $ResourceGroup) -ne 'true') {
    Write-Host "Resource group $ResourceGroup does not exist; nothing to delete."
    return
}

if (-not $Force) {
    $answer = Read-Host "Delete resource group '$ResourceGroup' and everything in it? Type the name to confirm"
    if ($answer -ne $ResourceGroup) {
        Write-Host 'Cancelled.'
        return
    }
}

az group delete --name $ResourceGroup --yes
if ($LASTEXITCODE -ne 0) { throw "Failed to delete $ResourceGroup" }
Write-Host "Deleted $ResourceGroup"
