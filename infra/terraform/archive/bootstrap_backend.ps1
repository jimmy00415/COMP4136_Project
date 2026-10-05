[CmdletBinding()]
param(
    [ValidateSet('Check', 'Ensure')]
    [string]$Mode = 'Check',

    [ValidateNotNullOrEmpty()]
    [string]$PreflightPath = (Join-Path $PSScriptRoot '..\..\..\artifacts\gcp\preflight.json'),

    [ValidateNotNullOrEmpty()]
    [string]$GcloudExecutable = 'gcloud',

    [ValidateNotNullOrEmpty()]
    [string]$InheritedIamAcceptancePath = (Join-Path $PSScriptRoot 'inherited_iam_acceptance.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedAccount = 'admin@motionexp.com'
$ExpectedProjectId = 'motionexpaiweb'
$ExpectedProjectNumber = '880586285913'
$ExpectedOrganizationId = '797368190621'
$ExpectedBucket = 'motionexpaiweb-880586285913-hk-movie-rag-tfstate'
$ExpectedBucketUri = "gs://$ExpectedBucket"
$ExpectedLocation = 'US-CENTRAL1'
$ExpectedSoftDeleteSeconds = 604800
$ExpectedLabels = [ordered]@{
    'managed-by' = 'hk-rag-backend-bootstrap'
    'purpose' = 'terraform-state'
}
$ExpectedTargetIamBindings = [ordered]@{
    'roles/storage.legacyBucketOwner' = @("user:$ExpectedAccount")
    'roles/storage.objectAdmin' = @("user:$ExpectedAccount")
}
$ExpectedDefaultIamBindings = [ordered]@{
    'roles/storage.legacyBucketOwner' = @(
        "projectEditor:$ExpectedProjectId",
        "projectOwner:$ExpectedProjectId"
    )
    'roles/storage.legacyBucketReader' = @("projectViewer:$ExpectedProjectId")
    'roles/storage.legacyObjectOwner' = @(
        "projectEditor:$ExpectedProjectId",
        "projectOwner:$ExpectedProjectId"
    )
    'roles/storage.legacyObjectReader' = @("projectViewer:$ExpectedProjectId")
}
$ExpectedAcceptanceRationale = @(
    'Existing application service accounts are accepted only through the exact reviewed snapshots.',
    'Broad user instruction accepts the current security posture captured by these snapshots.',
    'No IAM removal is authorized by this bootstrap.'
)

function Assert-ExactValue {
    param(
        [Parameter(Mandatory)]
        [string]$Name,

        [AllowNull()]
        [object]$Actual,

        [Parameter(Mandatory)]
        [object]$Expected
    )

    if ([string]$Actual -cne [string]$Expected) {
        throw "$Name mismatch: expected '$Expected', received '$Actual'"
    }
}

function Invoke-GcloudJson {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [switch]$AllowNotFound
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    if ($script:ResolvedGcloudCommand.CommandType -ceq 'ExternalScript') {
        if ([System.IO.Path]::GetFileName($script:ResolvedGcloudCommand.Source) -cne 'gcloud.ps1') {
            throw 'the gcloud PowerShell launcher must be named gcloud.ps1'
        }
        $sdkRoot = Split-Path (Split-Path $script:ResolvedGcloudCommand.Source -Parent) -Parent
        $gcloudPython = $env:CLOUDSDK_PYTHON
        if ([string]::IsNullOrWhiteSpace($gcloudPython)) {
            $gcloudPython = Join-Path $sdkRoot 'platform\bundledpython\python.exe'
        }
        if (-not (Test-Path -LiteralPath $gcloudPython -PathType Leaf)) {
            throw "gcloud Python executable not found: $gcloudPython"
        }
        $gcloudEntrypoint = Join-Path $sdkRoot 'lib\gcloud.py'
        if (-not (Test-Path -LiteralPath $gcloudEntrypoint -PathType Leaf)) {
            throw "gcloud Python entrypoint not found: $gcloudEntrypoint"
        }
        $startInfo.FileName = $gcloudPython
        if ([string]::IsNullOrWhiteSpace($env:CLOUDSDK_PYTHON_SITEPACKAGES)) {
            $null = $startInfo.ArgumentList.Add('-S')
        }
        $null = $startInfo.ArgumentList.Add($gcloudEntrypoint)
        $startInfo.Environment['CLOUDSDK_ROOT_DIR'] = $sdkRoot
        $startInfo.Environment['CLOUDSDK_GSUTIL_PYTHON'] = $gcloudPython
        $startInfo.Environment['PATH'] = (Join-Path $sdkRoot 'bin\sdk') + `
            [System.IO.Path]::PathSeparator + $env:PATH
    }
    elseif ($script:ResolvedGcloudCommand.CommandType -ceq 'Application') {
        if ([System.IO.Path]::GetExtension($script:ResolvedGcloudCommand.Source) -in @('.cmd', '.bat')) {
            throw 'gcloud must resolve to a native executable or PowerShell script, not a cmd/bat launcher'
        }
        $startInfo.FileName = $script:ResolvedGcloudCommand.Source
    }
    else {
        throw "unsupported gcloud command type: $($script:ResolvedGcloudCommand.CommandType)"
    }
    foreach ($argument in $Arguments) {
        $null = $startInfo.ArgumentList.Add($argument)
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw 'gcloud process did not start'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $outputText = $stdoutTask.GetAwaiter().GetResult()
        $errorText = $stderrTask.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
    }
    finally {
        $process.Dispose()
    }

    if ($exitCode -ne 0) {
        $notFoundPattern = `
            '(?i)(HTTPError\s+404|status(?:\s+code)?[=: ]+404|NotFoundException|not found:\s*404\.?\s*\z)'
        if ($AllowNotFound -and $errorText -match $notFoundPattern) {
            return $null
        }
        $sanitizedError = [regex]::Replace(
            $errorText,
            '[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]',
            '?'
        ).Trim()
        if ($sanitizedError.Length -gt 4096) {
            $sanitizedError = $sanitizedError.Substring(0, 4096) + '...[truncated]'
        }
        throw "gcloud failed with exit code $exitCode for argv [$($Arguments -join ', ')]: $sanitizedError"
    }

    if ([string]::IsNullOrWhiteSpace($outputText)) {
        throw "gcloud returned empty JSON for argv [$($Arguments -join ', ')]"
    }

    try {
        return $outputText | ConvertFrom-Json -Depth 100
    }
    catch {
        throw "gcloud returned invalid JSON for argv [$($Arguments -join ', ')]"
    }
}

function New-TemporaryJsonFile {
    param(
        [Parameter(Mandatory)]
        [object]$Value
    )

    $path = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("hk-rag-backend-{0}.json" -f [guid]::NewGuid().ToString('N'))
    $stream = [System.IO.FileStream]::new(
        $path,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::Read
    )
    try {
        $writer = [System.IO.StreamWriter]::new(
            $stream,
            [System.Text.UTF8Encoding]::new($false),
            1024,
            $true
        )
        try {
            $writer.Write(($Value | ConvertTo-Json -Depth 10 -Compress))
            $writer.Flush()
        }
        finally {
            $writer.Dispose()
        }
    }
    catch {
        $stream.Dispose()
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        throw
    }
    return [pscustomobject]@{
        Path = $path
        Stream = $stream
    }
}

function Assert-ExactPropertyNames {
    param(
        [Parameter(Mandatory)]
        [string]$Context,

        [Parameter(Mandatory)]
        [object]$Value,

        [Parameter(Mandatory)]
        [string[]]$Expected
    )

    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expectedSorted = @($Expected | Sort-Object)
    if ($actual.Count -ne $expectedSorted.Count -or `
            @(Compare-Object $expectedSorted $actual).Count -ne 0) {
        throw "$Context property set does not match the accepted schema"
    }
}

function ConvertTo-CanonicalIamPolicy {
    param(
        [Parameter(Mandatory)]
        [object]$Policy,

        [Parameter(Mandatory)]
        [string]$Context,

        [switch]$AllowEtag
    )

    $allowedPolicyProperties = @('bindings', 'version')
    if ($AllowEtag) {
        $allowedPolicyProperties += 'etag'
    }
    $actualPolicyProperties = @($Policy.PSObject.Properties.Name)
    foreach ($propertyName in $actualPolicyProperties) {
        if ($propertyName -notin $allowedPolicyProperties) {
            throw "$Context contains unexpected top-level field '$propertyName'"
        }
    }
    if ('bindings' -notin $actualPolicyProperties -or $null -eq $Policy.bindings) {
        throw "$Context is missing bindings"
    }
    $version = if ('version' -in $actualPolicyProperties) { [int]$Policy.version } else { 1 }
    if ($version -notin @(1, 3)) {
        throw "$Context has unsupported IAM policy version '$version'"
    }

    $bindingEntries = @()
    foreach ($binding in @($Policy.bindings)) {
        $bindingProperties = @($binding.PSObject.Properties.Name)
        foreach ($propertyName in $bindingProperties) {
            if ($propertyName -notin @('role', 'members', 'condition')) {
                throw "$Context binding contains unexpected field '$propertyName'"
            }
        }
        foreach ($requiredProperty in @('role', 'members')) {
            if ($requiredProperty -notin $bindingProperties) {
                throw "$Context binding is missing '$requiredProperty'"
            }
        }
        $role = [string]$binding.role
        if ([string]::IsNullOrWhiteSpace($role)) {
            throw "$Context binding role is empty"
        }
        $members = @($binding.members | ForEach-Object { [string]$_ } | Sort-Object)
        if ($members.Count -eq 0 -or @($members | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count -ne 0) {
            throw "$Context binding contains an empty member set or member"
        }
        if (@($members | Select-Object -Unique).Count -ne $members.Count) {
            throw "$Context binding contains duplicate members"
        }

        $canonicalBinding = [ordered]@{
            role = $role
            members = $members
        }
        if ('condition' -in $bindingProperties) {
            if ($null -eq $binding.condition) {
                throw "$Context binding condition is null"
            }
            $conditionProperties = @($binding.condition.PSObject.Properties.Name)
            foreach ($propertyName in $conditionProperties) {
                if ($propertyName -notin @('title', 'expression', 'description')) {
                    throw "$Context condition contains unexpected field '$propertyName'"
                }
            }
            foreach ($requiredProperty in @('title', 'expression')) {
                if ($requiredProperty -notin $conditionProperties -or `
                        [string]::IsNullOrWhiteSpace([string]$binding.condition.$requiredProperty)) {
                    throw "$Context condition is missing '$requiredProperty'"
                }
            }
            $canonicalCondition = [ordered]@{
                title = [string]$binding.condition.title
                expression = [string]$binding.condition.expression
            }
            if ('description' -in $conditionProperties) {
                $canonicalCondition.description = [string]$binding.condition.description
            }
            $canonicalBinding.condition = $canonicalCondition
        }
        $canonicalBindingObject = [pscustomobject]$canonicalBinding
        $bindingEntries += [pscustomobject]@{
            Json = ($canonicalBindingObject | ConvertTo-Json -Depth 20 -Compress)
            Value = $canonicalBindingObject
        }
    }
    $bindingJson = @($bindingEntries.Json)
    if (@($bindingJson | Select-Object -Unique).Count -ne $bindingJson.Count) {
        throw "$Context contains duplicate bindings"
    }
    $canonicalBindings = @(
        $bindingEntries | Sort-Object Json | ForEach-Object { $_.Value }
    )
    return [pscustomobject]([ordered]@{
            version = $version
            bindings = $canonicalBindings
        })
}

function Get-IamPolicyEvidence {
    param(
        [Parameter(Mandatory)]
        [object]$Policy,

        [Parameter(Mandatory)]
        [string]$Context,

        [switch]$AllowEtag
    )

    $canonical = ConvertTo-CanonicalIamPolicy `
        -Policy $Policy `
        -Context $Context `
        -AllowEtag:$AllowEtag
    $json = $canonical | ConvertTo-Json -Depth 100 -Compress
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($json))
    }
    finally {
        $sha256.Dispose()
    }
    return [pscustomobject]@{
        Canonical = $canonical
        Json = $json
        Sha256 = ([System.BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
    }
}

function Get-InheritedIamAcceptance {
    $resolvedPath = (Resolve-Path -LiteralPath $InheritedIamAcceptancePath -ErrorAction Stop).Path
    $contract = Get-Content -Raw -LiteralPath $resolvedPath | ConvertFrom-Json -Depth 100
    Assert-ExactPropertyNames `
        -Context 'inherited IAM acceptance' `
        -Value $contract `
        -Expected @(
            'schema_version', 'status', 'project', 'accepted_principals', 'rationale',
            'project_policy', 'organization_policy'
        )
    Assert-ExactValue -Name 'acceptance.schema_version' -Actual $contract.schema_version -Expected 1
    Assert-ExactPropertyNames `
        -Context 'acceptance.project' `
        -Value $contract.project `
        -Expected @('id', 'number', 'parent')
    Assert-ExactValue -Name 'acceptance.project.id' -Actual $contract.project.id -Expected $ExpectedProjectId
    Assert-ExactValue `
        -Name 'acceptance.project.number' `
        -Actual $contract.project.number `
        -Expected $ExpectedProjectNumber
    Assert-ExactPropertyNames `
        -Context 'acceptance.project.parent' `
        -Value $contract.project.parent `
        -Expected @('type', 'id')
    Assert-ExactValue `
        -Name 'acceptance.project.parent.type' `
        -Actual $contract.project.parent.type `
        -Expected 'organization'
    Assert-ExactValue `
        -Name 'acceptance.project.parent.id' `
        -Actual $contract.project.parent.id `
        -Expected $ExpectedOrganizationId
    $rationale = @($contract.rationale | ForEach-Object { [string]$_ })
    if ($rationale.Count -ne $ExpectedAcceptanceRationale.Count -or `
            @(Compare-Object $ExpectedAcceptanceRationale $rationale).Count -ne 0) {
        throw 'inherited IAM acceptance rationale does not match the governed decision'
    }
    if ([string]$contract.status -cne 'accepted') {
        throw "inherited IAM acceptance is not ready: status '$($contract.status)'"
    }

    foreach ($scope in @('project', 'organization')) {
        $record = $contract."${scope}_policy"
        if ($null -eq $record) {
            throw "accepted inherited IAM contract is missing ${scope}_policy"
        }
        Assert-ExactPropertyNames `
            -Context "acceptance.${scope}_policy" `
            -Value $record `
            -Expected @('sha256', 'policy')
        if ([string]$record.sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw "acceptance.${scope}_policy.sha256 is invalid"
        }
        $evidence = Get-IamPolicyEvidence `
            -Policy $record.policy `
            -Context "acceptance.${scope}_policy.policy"
        Assert-ExactValue `
            -Name "acceptance.${scope}_policy.sha256" `
            -Actual $record.sha256 `
            -Expected $evidence.Sha256
        $record | Add-Member -NotePropertyName CanonicalJson -NotePropertyValue $evidence.Json
    }

    $expectedPrincipals = @(
        @($contract.project_policy.policy.bindings) +
        @($contract.organization_policy.policy.bindings) |
        ForEach-Object { @($_.members) } |
        ForEach-Object { [string]$_ } |
        Sort-Object -Unique
    )
    $acceptedPrincipals = @(
        $contract.accepted_principals | ForEach-Object { [string]$_ } | Sort-Object
    )
    if ($acceptedPrincipals.Count -ne $expectedPrincipals.Count -or `
            @(Compare-Object $expectedPrincipals $acceptedPrincipals).Count -ne 0) {
        throw 'accepted_principals does not exactly list the snapshot principals'
    }
    return $contract
}

function Assert-InheritedIamPolicyMatches {
    param(
        [Parameter(Mandatory)]
        [string]$Scope,

        [Parameter(Mandatory)]
        [object]$LivePolicy,

        [Parameter(Mandatory)]
        [object]$AcceptedRecord
    )

    $liveEvidence = Get-IamPolicyEvidence `
        -Policy $LivePolicy `
        -Context "live $Scope IAM policy" `
        -AllowEtag
    Assert-ExactValue `
        -Name "live $Scope IAM policy digest" `
        -Actual $liveEvidence.Sha256 `
        -Expected $AcceptedRecord.sha256
    Assert-ExactValue `
        -Name "live $Scope IAM canonical snapshot" `
        -Actual $liveEvidence.Json `
        -Expected $AcceptedRecord.CanonicalJson
}

function Assert-BucketMetadata {
    param(
        [Parameter(Mandatory)]
        [object]$Bucket
    )

    Assert-ExactValue -Name 'bucket.name' -Actual $Bucket.name -Expected $ExpectedBucket
    Assert-ExactValue -Name 'bucket.projectNumber' -Actual $Bucket.projectNumber -Expected $ExpectedProjectNumber
    Assert-ExactValue -Name 'bucket.location' -Actual $Bucket.location -Expected $ExpectedLocation

    if ($Bucket.iamConfiguration.uniformBucketLevelAccess.enabled -ne $true) {
        throw 'bucket uniform bucket-level access is not enabled'
    }
    Assert-ExactValue `
        -Name 'bucket public access prevention' `
        -Actual $Bucket.iamConfiguration.publicAccessPrevention `
        -Expected 'enforced'
    Assert-ExactValue `
        -Name 'bucket soft delete seconds' `
        -Actual $Bucket.softDeletePolicy.retentionDurationSeconds `
        -Expected $ExpectedSoftDeleteSeconds

    $labelsProperty = $Bucket.PSObject.Properties['labels']
    if ($null -eq $labelsProperty -or $null -eq $labelsProperty.Value) {
        throw 'bucket does not contain the bootstrap provenance labels'
    }
    $actualLabelNames = @($labelsProperty.Value.PSObject.Properties.Name | Sort-Object)
    $expectedLabelNames = @($ExpectedLabels.Keys | Sort-Object)
    if (@(Compare-Object $expectedLabelNames $actualLabelNames).Count -ne 0) {
        throw 'bucket bootstrap provenance label set does not match'
    }
    foreach ($labelName in $ExpectedLabels.Keys) {
        Assert-ExactValue `
            -Name "bucket label $labelName" `
            -Actual $labelsProperty.Value.$labelName `
            -Expected $ExpectedLabels[$labelName]
    }

    $retentionPolicy = $Bucket.PSObject.Properties['retentionPolicy']
    if ($null -ne $retentionPolicy -and $null -ne $retentionPolicy.Value) {
        throw 'bucket has a retention policy; the backend must not use retention lock'
    }

}

function Test-IamBindingsExact {
    param(
        [Parameter(Mandatory)]
        [object[]]$Bindings,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Expected
    )

    if ($Bindings.Count -ne $Expected.Count) {
        return $false
    }

    $actualByRole = @{}
    foreach ($binding in $Bindings) {
        $propertyNames = @($binding.PSObject.Properties.Name | Sort-Object)
        if (@(Compare-Object @('members', 'role') $propertyNames).Count -ne 0) {
            return $false
        }
        $role = [string]$binding.role
        if ([string]::IsNullOrWhiteSpace($role) -or $actualByRole.ContainsKey($role)) {
            return $false
        }
        $actualByRole[$role] = @($binding.members | ForEach-Object { [string]$_ } | Sort-Object)
    }

    foreach ($role in $Expected.Keys) {
        if (-not $actualByRole.ContainsKey($role)) {
            return $false
        }
        $expectedMembers = @($Expected[$role] | Sort-Object)
        $actualMembers = @($actualByRole[$role])
        if ($actualMembers.Count -ne $expectedMembers.Count -or `
                @(Compare-Object $expectedMembers $actualMembers).Count -ne 0) {
            return $false
        }
    }
    return $true
}

function Get-IamPolicyState {
    param(
        [Parameter(Mandatory)]
        [object]$IamPolicy
    )

    $propertyNames = @($IamPolicy.PSObject.Properties.Name | Sort-Object)
    foreach ($propertyName in $propertyNames) {
        if ($propertyName -notin @('bindings', 'etag', 'version')) {
            throw "bucket IAM contains unexpected top-level field '$propertyName'"
        }
    }
    foreach ($requiredProperty in @('bindings', 'etag')) {
        if ($requiredProperty -notin $propertyNames) {
            throw "bucket IAM is missing required field '$requiredProperty'"
        }
    }
    if ([string]::IsNullOrWhiteSpace([string]$IamPolicy.etag)) {
        throw 'bucket IAM etag is empty'
    }
    if ('version' -in $propertyNames -and [int]$IamPolicy.version -ne 1) {
        throw 'bucket IAM version must be 1 when present'
    }

    $bindings = @($IamPolicy.bindings)
    if (Test-IamBindingsExact -Bindings $bindings -Expected $ExpectedTargetIamBindings) {
        return 'target'
    }
    if (Test-IamBindingsExact -Bindings $bindings -Expected $ExpectedDefaultIamBindings) {
        return 'default'
    }
    throw 'bucket direct IAM policy is neither the exact target nor the exact new-bucket default'
}

function Get-Bucket {
    return Invoke-GcloudJson -AllowNotFound -Arguments @(
        'storage', 'buckets', 'describe', $ExpectedBucketUri,
        "--project=$ExpectedProjectId", '--raw', '--format=json'
    )
}

function Get-BucketIamPolicy {
    return Invoke-GcloudJson -Arguments @(
        'storage', 'buckets', 'get-iam-policy', $ExpectedBucketUri,
        "--project=$ExpectedProjectId", '--format=json'
    )
}

function Set-TargetBucketIamPolicy {
    param(
        [Parameter(Mandatory)]
        [object]$CurrentPolicy
    )

    $policyFile = New-TemporaryJsonFile -Value ([ordered]@{
            bindings = @(
                [ordered]@{
                    role = 'roles/storage.legacyBucketOwner'
                    members = @("user:$ExpectedAccount")
                },
                [ordered]@{
                    role = 'roles/storage.objectAdmin'
                    members = @("user:$ExpectedAccount")
                }
            )
            version = 1
        })
    try {
        $null = Invoke-GcloudJson -Arguments @(
            'storage', 'buckets', 'set-iam-policy', $ExpectedBucketUri, $policyFile.Path,
            "--project=$ExpectedProjectId", "--account=$ExpectedAccount",
            "--etag=$($CurrentPolicy.etag)", '--quiet', '--format=json'
        )
    }
    finally {
        $policyFile.Stream.Dispose()
        Remove-Item -LiteralPath $policyFile.Path -Force -ErrorAction SilentlyContinue
    }

    $postPolicy = Get-BucketIamPolicy
    if ((Get-IamPolicyState -IamPolicy $postPolicy) -cne 'target') {
        throw 'bucket IAM hardening did not produce the exact target policy'
    }
}

function Write-Report {
    param(
        [Parameter(Mandatory)]
        [string]$Status,

        [Parameter(Mandatory)]
        [bool]$Mutated
    )

    [pscustomobject]@{
        bucket = $ExpectedBucket
        mode = $Mode
        mutated = $Mutated
        status = $Status
    } | ConvertTo-Json -Compress
}

$script:ResolvedGcloudCommand = Get-Command -Name $GcloudExecutable -ErrorAction SilentlyContinue
if ($null -eq $script:ResolvedGcloudCommand) {
    throw "gcloud executable not found: $GcloudExecutable"
}

$resolvedPreflight = (Resolve-Path -LiteralPath $PreflightPath -ErrorAction Stop).Path
$preflight = Get-Content -Raw -LiteralPath $resolvedPreflight | ConvertFrom-Json -Depth 100
Assert-ExactValue -Name 'preflight.active_account' -Actual $preflight.active_account -Expected $ExpectedAccount
Assert-ExactValue -Name 'preflight.active_project' -Actual $preflight.active_project -Expected $ExpectedProjectId
Assert-ExactValue -Name 'preflight.project_number' -Actual $preflight.project_number -Expected $ExpectedProjectNumber
Assert-ExactValue -Name 'preflight.project_lifecycle' -Actual $preflight.project_lifecycle -Expected 'ACTIVE'
if ($preflight.billing_enabled -ne $true) {
    throw 'preflight billing is not enabled'
}
if ($preflight.ready_for_archive -ne $true) {
    throw 'preflight is not ready for archive infrastructure'
}
if (@($preflight.blockers).Count -ne 0) {
    throw 'preflight contains blockers'
}
$inheritedIamAcceptance = Get-InheritedIamAcceptance

$activeAccounts = @(Invoke-GcloudJson -Arguments @(
        'auth', 'list', '--filter=status:ACTIVE', '--format=json'
    ))
if ($activeAccounts.Count -ne 1) {
    throw "expected exactly one active gcloud account, received $($activeAccounts.Count)"
}
Assert-ExactValue -Name 'active gcloud account' -Actual $activeAccounts[0].account -Expected $ExpectedAccount

$configuration = Invoke-GcloudJson -Arguments @('config', 'list', '--format=json')
Assert-ExactValue -Name 'configured gcloud account' -Actual $configuration.core.account -Expected $ExpectedAccount
Assert-ExactValue -Name 'configured gcloud project' -Actual $configuration.core.project -Expected $ExpectedProjectId

$project = Invoke-GcloudJson -Arguments @(
    'projects', 'describe', $ExpectedProjectId, '--format=json'
)
Assert-ExactValue -Name 'live project ID' -Actual $project.projectId -Expected $ExpectedProjectId
Assert-ExactValue -Name 'live project number' -Actual $project.projectNumber -Expected $ExpectedProjectNumber
Assert-ExactValue -Name 'live project lifecycle' -Actual $project.lifecycleState -Expected 'ACTIVE'
Assert-ExactValue -Name 'live project parent type' -Actual $project.parent.type -Expected 'organization'
Assert-ExactValue `
    -Name 'live project parent organization' `
    -Actual $project.parent.id `
    -Expected $ExpectedOrganizationId

$billing = Invoke-GcloudJson -Arguments @(
    'billing', 'projects', 'describe', $ExpectedProjectId, '--format=json'
)
Assert-ExactValue -Name 'billing project ID' -Actual $billing.projectId -Expected $ExpectedProjectId
if ($billing.billingEnabled -ne $true) {
    throw 'live project billing is not enabled'
}

$liveProjectIam = Invoke-GcloudJson -Arguments @(
    'projects', 'get-iam-policy', $ExpectedProjectId,
    "--account=$ExpectedAccount", '--format=json'
)
Assert-InheritedIamPolicyMatches `
    -Scope 'project' `
    -LivePolicy $liveProjectIam `
    -AcceptedRecord $inheritedIamAcceptance.project_policy

$liveOrganizationIam = Invoke-GcloudJson -Arguments @(
    'organizations', 'get-iam-policy', $ExpectedOrganizationId,
    "--account=$ExpectedAccount", '--format=json'
)
Assert-InheritedIamPolicyMatches `
    -Scope 'organization' `
    -LivePolicy $liveOrganizationIam `
    -AcceptedRecord $inheritedIamAcceptance.organization_policy

$bucket = Get-Bucket
if ($null -ne $bucket) {
    $iamPolicy = Get-BucketIamPolicy
    Assert-BucketMetadata -Bucket $bucket
    $iamState = Get-IamPolicyState -IamPolicy $iamPolicy
    $needsVersioning = $bucket.versioning.enabled -ne $true
    $needsIamHardening = $iamState -ceq 'default'

    if (-not $needsVersioning -and -not $needsIamHardening) {
        Write-Report -Status 'ready_existing' -Mutated $false
        exit 0
    }

    if ($Mode -eq 'Check') {
        if ($needsVersioning -and $needsIamHardening) {
            Write-Report -Status 'would_recover_versioning_and_harden_iam' -Mutated $false
        }
        elseif ($needsVersioning) {
            Write-Report -Status 'would_recover_versioning' -Mutated $false
        }
        else {
            Write-Report -Status 'would_harden_iam' -Mutated $false
        }
        exit 0
    }

    if ($needsVersioning) {
        $null = Invoke-GcloudJson -Arguments @(
            'storage', 'buckets', 'update', $ExpectedBucketUri,
            "--project=$ExpectedProjectId", "--account=$ExpectedAccount",
            '--versioning', '--quiet', '--format=json'
        )
        $recoveredBucket = Get-Bucket
        if ($null -eq $recoveredBucket) {
            throw 'backend bucket was not visible after versioning recovery'
        }
        Assert-BucketMetadata -Bucket $recoveredBucket
        if ($recoveredBucket.versioning.enabled -ne $true) {
            throw 'backend bucket versioning recovery did not take effect'
        }
        $iamPolicy = Get-BucketIamPolicy
        $iamState = Get-IamPolicyState -IamPolicy $iamPolicy
        if ($needsIamHardening -and $iamState -cne 'default') {
            throw 'bucket IAM changed before the approved hardening step'
        }
        if (-not $needsIamHardening -and $iamState -cne 'target') {
            throw 'bucket IAM changed during versioning recovery'
        }
    }

    if ($needsIamHardening) {
        Set-TargetBucketIamPolicy -CurrentPolicy $iamPolicy
    }

    if ($needsVersioning -and $needsIamHardening) {
        Write-Report -Status 'recovered_versioning_and_hardened_iam' -Mutated $true
    }
    elseif ($needsVersioning) {
        Write-Report -Status 'recovered_versioning' -Mutated $true
    }
    else {
        Write-Report -Status 'hardened_iam' -Mutated $true
    }
    exit 0
}

if ($Mode -eq 'Check') {
    Write-Report -Status 'would_create' -Mutated $false
    exit 0
}

$null = Invoke-GcloudJson -Arguments @(
    'storage', 'buckets', 'create', $ExpectedBucketUri,
    "--project=$ExpectedProjectId", "--account=$ExpectedAccount",
    "--location=$ExpectedLocation",
    '--uniform-bucket-level-access', '--public-access-prevention',
    '--soft-delete-duration=7d', '--quiet', '--format=json'
)

$labelsFile = New-TemporaryJsonFile -Value $ExpectedLabels
try {
    $null = Invoke-GcloudJson -Arguments @(
        'storage', 'buckets', 'update', $ExpectedBucketUri,
        "--project=$ExpectedProjectId", "--account=$ExpectedAccount",
        '--versioning', "--labels-file=$($labelsFile.Path)", '--quiet', '--format=json'
    )
}
finally {
    $labelsFile.Stream.Dispose()
    Remove-Item -LiteralPath $labelsFile.Path -Force -ErrorAction SilentlyContinue
}

$createdBucket = Get-Bucket
if ($null -eq $createdBucket) {
    throw 'backend bucket was not visible after creation'
}
Assert-BucketMetadata -Bucket $createdBucket
if ($createdBucket.versioning.enabled -ne $true) {
    throw 'backend bucket versioning is not enabled after creation'
}
$createdIamPolicy = Get-BucketIamPolicy
$createdIamState = Get-IamPolicyState -IamPolicy $createdIamPolicy
if ($createdIamState -ceq 'default') {
    Set-TargetBucketIamPolicy -CurrentPolicy $createdIamPolicy
}
Write-Report -Status 'created' -Mutated $true
