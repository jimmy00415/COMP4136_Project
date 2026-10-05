[CmdletBinding()]
param(
    [ValidateSet(
        'Check', 'GrantTemporary', 'RepairBucket', 'RevokeTemporary',
        'CheckArchiveOperator', 'GrantArchiveOperator'
    )]
    [string]$Mode = 'Check',

    [ValidateNotNullOrEmpty()]
    [string]$PreflightPath = (Join-Path $PSScriptRoot '..\..\..\artifacts\gcp\preflight.json'),

    [ValidateNotNullOrEmpty()]
    [string]$GcloudExecutable = 'gcloud',

    [ValidateNotNullOrEmpty()]
    [string]$InheritedIamAcceptancePath = (Join-Path $PSScriptRoot 'inherited_iam_acceptance.json'),

    [ValidateRange(1, 60)]
    [int]$MaxProbeAttempts = 12,

    [ValidateRange(0, 30)]
    [int]$ProbeIntervalSeconds = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExpectedAccount = 'admin@motionexp.com'
$ExpectedProjectId = 'motionexpaiweb'
$ExpectedProjectNumber = '880586285913'
$ExpectedOrganizationId = '797368190621'
$ExpectedBucket = 'motionexpaiweb-880586285913-hk-movie-rag-tfstate'
$ExpectedBucketUri = "gs://$ExpectedBucket"
$ExpectedBucketResourceName = "projects/_/buckets/$ExpectedBucket"
$ExpectedLocation = 'US-CENTRAL1'
$ExpectedSoftDeleteSeconds = 604800
$MinimumConditionCapableGcloudMajor = 263
$ExpectedLabels = [ordered]@{
    'managed-by' = 'hk-rag-backend-bootstrap'
    'purpose' = 'terraform-state'
}
$ExpectedLockoutIamBindings = [ordered]@{
    'roles/storage.objectAdmin' = @("user:$ExpectedAccount")
}
$ExpectedDurableIamBindings = [ordered]@{
    'roles/storage.legacyBucketOwner' = @("user:$ExpectedAccount")
    'roles/storage.objectAdmin' = @("user:$ExpectedAccount")
}
$TemporaryBindingTitle = 'hk-rag-backend-iam-recovery'
$TemporaryBindingDescription = 'Temporary bucket-scoped access for approved backend IAM recovery.'
$TemporaryBindingExpression = `
    'resource.type == "storage.googleapis.com/Bucket" && ' + `
    "resource.name == `"$ExpectedBucketResourceName`""
$ExpectedArchiveBucket = 'motionexpaiweb-880586285913-hk-movie-rag-source-archive'
$ExpectedArchiveBucketUri = "gs://$ExpectedArchiveBucket"
$ExpectedArchiveBucketResourceName = "projects/_/buckets/$ExpectedArchiveBucket"
$ExpectedArchiveWriter = `
    'serviceAccount:hk-rag-archive-writer@motionexpaiweb.iam.gserviceaccount.com'
$ExpectedArchiveLabels = [ordered]@{
    'goog-terraform-provisioned' = 'true'
}
$ExpectedArchiveIamBindings = [ordered]@{
    'roles/storage.objectCreator' = @($ExpectedArchiveWriter)
    'roles/storage.objectViewer' = @($ExpectedArchiveWriter)
}
$ArchiveOperatorBindingTitle = 'hk-rag-archive-operator'
$ArchiveOperatorBindingDescription = `
    'Persistent bucket-scoped management access for the HK Movie RAG source archive.'
$ArchiveOperatorBindingExpression = `
    'resource.type == "storage.googleapis.com/Bucket" && ' + `
    "resource.name == `"$ExpectedArchiveBucketResourceName`""
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

function Write-BoundedWarning {
    param(
        [Parameter(Mandatory)]
        [string]$Message
    )

    $sanitized = [regex]::Replace(
        $Message,
        '[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]',
        '?'
    ).Trim()
    if ($sanitized.Length -gt 512) {
        $sanitized = $sanitized.Substring(0, 512) + '...[truncated]'
    }
    [Console]::Error.WriteLine("WARNING: $sanitized")
}

function Remove-TemporaryJsonFile {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    try {
        Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    catch {
        Write-BoundedWarning `
            -Message "temporary recovery file cleanup failed for '$Path': $($_.Exception.Message)"
    }
}

function Test-ExactBucketPermissionDenied {
    param(
        [Parameter(Mandatory)]
        [string]$ErrorText,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $commandName = $null
    $permission = $null
    if ($Arguments[0..2] -join ' ' -ceq 'storage buckets describe') {
        $commandName = 'describe'
        $permission = 'storage.buckets.get'
    }
    elseif ($Arguments[0..2] -join ' ' -ceq 'storage buckets get-iam-policy') {
        $commandName = 'get-iam-policy'
        $permission = 'storage.buckets.getIamPolicy'
    }
    else {
        return $false
    }

    if ($Arguments.Count -lt 4) {
        return $false
    }
    $requestedBucketUri = [string]$Arguments[3]
    $requestedBucket = if ($requestedBucketUri.StartsWith('gs://')) {
        $requestedBucketUri.Substring(5)
    }
    else {
        return $false
    }
    if ($requestedBucket -notin @($ExpectedBucket, $ExpectedArchiveBucket)) {
        return $false
    }

    $account = [regex]::Escape($ExpectedAccount)
    $bucket = [regex]::Escape($requestedBucket)
    $resourceName = "projects/_/buckets/$requestedBucket"
    $resource = [regex]::Escape("//storage.googleapis.com/$resourceName")
    $permissionPattern = [regex]::Escape($permission)
    $commandPattern = [regex]::Escape($commandName)
    $pattern = "(?s)\AERROR: \(gcloud\.storage\.buckets\.$commandPattern\) " +
        "\[$account\] does not have permission to access b instance \[$bucket\] " +
        "\(or it may not exist\): $account does not have $permissionPattern access to the " +
        "Google Cloud Storage bucket\. Permission '$permissionPattern' denied on resource " +
        "'$resource' \(or it may not exist\)\. Remediate access with this Troubleshooter URL " +
        "or share it with your administrator - https://console\.cloud\.google\.com/iam-admin/" +
        "troubleshooter/summary;errorId=[^\s]+ \. This command is authenticated as $account " +
        "which is the active account specified by the \[core/account\] property\.\s*\z"
    return $ErrorText -cmatch $pattern
}

function Invoke-GcloudJson {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [switch]$AllowExactBucketPermissionDenied
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
        if ($AllowExactBucketPermissionDenied -and `
                (Test-ExactBucketPermissionDenied -ErrorText $errorText -Arguments $Arguments)) {
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

function Assert-ConditionCapableGcloudVersion {
    $versionDocument = Invoke-GcloudJson -Arguments @('version', '--format=json')
    $versionProperty = $versionDocument.PSObject.Properties['Google Cloud SDK']
    if ($null -eq $versionProperty) {
        throw 'gcloud version JSON is missing the Google Cloud SDK version'
    }
    $versionText = [string]$versionProperty.Value
    if ($versionText -cnotmatch '^(?<major>[0-9]+)\.(?<minor>[0-9]+)\.(?<patch>[0-9]+)$') {
        throw "gcloud version is not an exact semantic version: '$versionText'"
    }
    $major = [int]$Matches.major
    if ($major -lt $MinimumConditionCapableGcloudMajor) {
        throw "gcloud $versionText cannot safely read conditional IAM; version 263.0.0 or newer is required"
    }
}

function New-TemporaryJsonFile {
    param(
        [Parameter(Mandatory)]
        [object]$Value
    )

    $path = Join-Path `
        ([System.IO.Path]::GetTempPath()) `
        ("hk-rag-backend-recovery-{0}.json" -f [guid]::NewGuid().ToString('N'))
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
            $writer.Write(($Value | ConvertTo-Json -Depth 100 -Compress))
            $writer.Flush()
        }
        finally {
            $writer.Dispose()
        }
    }
    catch {
        $stream.Dispose()
        Remove-TemporaryJsonFile -Path $path
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
        $members = @($binding.members | ForEach-Object { [string]$_ } | Sort-Object)
        if ([string]::IsNullOrWhiteSpace($role) -or $members.Count -eq 0 -or `
                @($members | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count -ne 0) {
            throw "$Context binding contains an empty role, member set, or member"
        }
        if (@($members | Select-Object -Unique).Count -ne $members.Count) {
            throw "$Context binding contains duplicate members"
        }

        $canonicalBinding = [ordered]@{ role = $role; members = $members }
        if ('condition' -in $bindingProperties) {
            if ($null -eq $binding.condition) {
                throw "$Context binding condition is null"
            }
            $conditionProperties = @($binding.condition.PSObject.Properties.Name)
            foreach ($propertyName in $conditionProperties) {
                if ($propertyName -notin @('title', 'expression', 'description', 'location')) {
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
            if ('location' -in $conditionProperties) {
                $canonicalCondition.location = [string]$binding.condition.location
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
    return [pscustomobject]([ordered]@{ version = $version; bindings = $canonicalBindings })
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
    Assert-ExactValue -Name 'acceptance.project.number' -Actual $contract.project.number -Expected $ExpectedProjectNumber
    Assert-ExactPropertyNames `
        -Context 'acceptance.project.parent' `
        -Value $contract.project.parent `
        -Expected @('type', 'id')
    Assert-ExactValue -Name 'acceptance.project.parent.type' -Actual $contract.project.parent.type -Expected 'organization'
    Assert-ExactValue -Name 'acceptance.project.parent.id' -Actual $contract.project.parent.id -Expected $ExpectedOrganizationId
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

function Test-InheritedIamPolicyMatches {
    param(
        [Parameter(Mandatory)]
        [object]$LivePolicy,

        [Parameter(Mandatory)]
        [object]$AcceptedRecord
    )

    try {
        $liveEvidence = Get-IamPolicyEvidence `
            -Policy $LivePolicy `
            -Context 'live IAM policy after temporary cleanup' `
            -AllowEtag
        return $liveEvidence.Sha256 -ceq [string]$AcceptedRecord.sha256 -and `
            $liveEvidence.Json -ceq [string]$AcceptedRecord.CanonicalJson
    }
    catch {
        return $false
    }
}

function New-TemporaryBinding {
    return [pscustomobject]([ordered]@{
            role = 'roles/storage.admin'
            members = @("user:$ExpectedAccount")
            condition = [pscustomobject]([ordered]@{
                    title = $TemporaryBindingTitle
                    description = $TemporaryBindingDescription
                    expression = $TemporaryBindingExpression
                })
        })
}

function Test-ExactTemporaryBinding {
    param(
        [Parameter(Mandatory)]
        [object]$Binding
    )

    $properties = @($Binding.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object @('condition', 'members', 'role') $properties).Count -ne 0) {
        return $false
    }
    if ([string]$Binding.role -cne 'roles/storage.admin') {
        return $false
    }
    $members = @($Binding.members | ForEach-Object { [string]$_ })
    if ($members.Count -ne 1 -or $members[0] -cne "user:$ExpectedAccount") {
        return $false
    }
    if ($null -eq $Binding.condition) {
        return $false
    }
    $conditionProperties = @($Binding.condition.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object @('description', 'expression', 'title') $conditionProperties).Count -ne 0) {
        return $false
    }
    return [string]$Binding.condition.title -ceq $TemporaryBindingTitle -and `
        [string]$Binding.condition.description -ceq $TemporaryBindingDescription -and `
        [string]$Binding.condition.expression -ceq $TemporaryBindingExpression
}

function New-ArchiveOperatorBinding {
    return [pscustomobject]([ordered]@{
            role = 'roles/storage.admin'
            members = @("user:$ExpectedAccount")
            condition = [pscustomobject]([ordered]@{
                    title = $ArchiveOperatorBindingTitle
                    description = $ArchiveOperatorBindingDescription
                    expression = $ArchiveOperatorBindingExpression
                })
        })
}

function Test-ExactArchiveOperatorBinding {
    param(
        [Parameter(Mandatory)]
        [object]$Binding
    )

    $properties = @($Binding.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object @('condition', 'members', 'role') $properties).Count -ne 0) {
        return $false
    }
    if ([string]$Binding.role -cne 'roles/storage.admin') {
        return $false
    }
    $members = @($Binding.members | ForEach-Object { [string]$_ })
    if ($members.Count -ne 1 -or $members[0] -cne "user:$ExpectedAccount") {
        return $false
    }
    if ($null -eq $Binding.condition) {
        return $false
    }
    $conditionProperties = @($Binding.condition.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object @('description', 'expression', 'title') $conditionProperties).Count -ne 0) {
        return $false
    }
    return [string]$Binding.condition.title -ceq $ArchiveOperatorBindingTitle -and `
        [string]$Binding.condition.description -ceq $ArchiveOperatorBindingDescription -and `
        [string]$Binding.condition.expression -ceq $ArchiveOperatorBindingExpression
}

function Get-ArchiveOperatorAcceptedPolicies {
    param(
        [Parameter(Mandatory)]
        [object]$Acceptance
    )

    if ([int]$Acceptance.project_policy.policy.version -ne 3) {
        throw 'archive operator acceptance requires project IAM policy version 3'
    }
    $operatorBindings = @(
        @($Acceptance.project_policy.policy.bindings) |
        Where-Object { Test-ExactArchiveOperatorBinding -Binding $_ }
    )
    if ($operatorBindings.Count -ne 1) {
        throw "archive operator acceptance requires exactly one exact approved binding; found $($operatorBindings.Count)"
    }
    $sameTitleBindings = @(
        @($Acceptance.project_policy.policy.bindings) |
        Where-Object {
            'condition' -in @($_.PSObject.Properties.Name) -and `
                [string]$_.condition.title -ceq $ArchiveOperatorBindingTitle
        }
    )
    if ($sameTitleBindings.Count -ne 1) {
        throw 'archive operator acceptance contains a conflicting condition title'
    }

    $baselineBindings = @(
        @($Acceptance.project_policy.policy.bindings) |
        Where-Object { -not (Test-ExactArchiveOperatorBinding -Binding $_) }
    )
    $baselineHasConditions = @(
        $baselineBindings |
        Where-Object { 'condition' -in @($_.PSObject.Properties.Name) }
    ).Count -gt 0
    $baselineVersion = if ($baselineHasConditions) { 3 } else { 1 }
    $baselinePolicy = [pscustomobject]([ordered]@{
            version = $baselineVersion
            bindings = $baselineBindings
        })
    $baselineEvidence = Get-IamPolicyEvidence `
        -Policy $baselinePolicy `
        -Context 'archive operator accepted pre-grant project IAM policy'
    return [pscustomobject]@{
        Baseline = $baselineEvidence
        Target = [pscustomobject]@{
            CanonicalJson = [string]$Acceptance.project_policy.CanonicalJson
            Policy = $Acceptance.project_policy.policy
        }
    }
}

function Get-ArchiveOperatorProjectPolicyState {
    param(
        [Parameter(Mandatory)]
        [object]$Policy,

        [Parameter(Mandatory)]
        [object]$AcceptedPolicies
    )

    Assert-NoWithcondPlaceholder -Policy $Policy
    $properties = @($Policy.PSObject.Properties.Name)
    if ('etag' -notin $properties -or [string]::IsNullOrWhiteSpace([string]$Policy.etag)) {
        throw 'live project IAM policy is missing a non-empty etag'
    }
    $evidence = Get-IamPolicyEvidence `
        -Policy $Policy `
        -Context 'live archive operator project IAM policy' `
        -AllowEtag
    if ($evidence.Json -ceq $AcceptedPolicies.Target.CanonicalJson) {
        return 'target'
    }
    if ($evidence.Json -ceq $AcceptedPolicies.Baseline.Json) {
        return 'baseline'
    }
    throw 'live project IAM is neither the exact approved pre-grant baseline nor the exact archive operator target'
}

function New-ArchiveOperatorProjectPolicy {
    param(
        [Parameter(Mandatory)]
        [object]$AcceptedPolicies,

        [Parameter(Mandatory)]
        [string]$Etag
    )

    return [pscustomobject]([ordered]@{
            bindings = @($AcceptedPolicies.Target.Policy.bindings)
            etag = $Etag
            version = 3
        })
}

function Assert-NoWithcondPlaceholder {
    param(
        [Parameter(Mandatory)]
        [object]$Policy
    )

    $placeholderPattern = `
        '^(?:roles/[A-Za-z0-9_.-]+|(?:projects|organizations)/' + `
        '[A-Za-z0-9_.-]+/roles/[A-Za-z0-9_.-]+)_withcond_[A-Fa-f0-9]+$'
    foreach ($binding in @($Policy.bindings)) {
        if ([string]$binding.role -cmatch $placeholderPattern) {
            throw "IAM policy contains a lossy _withcond_ placeholder role '$($binding.role)'; conditional bindings are not safe to mutate"
        }
    }
}

function ConvertTo-CanonicalAuditConfigs {
    param(
        [Parameter(Mandatory)]
        [object[]]$AuditConfigs,

        [Parameter(Mandatory)]
        [string]$Context
    )

    $configEntries = @()
    foreach ($auditConfig in $AuditConfigs) {
        Assert-ExactPropertyNames `
            -Context "$Context audit config" `
            -Value $auditConfig `
            -Expected @('auditLogConfigs', 'service')
        if ([string]::IsNullOrWhiteSpace([string]$auditConfig.service)) {
            throw "$Context audit config service is empty"
        }
        $logEntries = @()
        foreach ($logConfig in @($auditConfig.auditLogConfigs)) {
            $properties = @($logConfig.PSObject.Properties.Name | Sort-Object)
            if (@(Compare-Object @('logType') $properties).Count -ne 0 -and `
                    @(Compare-Object @('exemptedMembers', 'logType') $properties).Count -ne 0) {
                throw "$Context audit log config property set is not supported"
            }
            if ([string]::IsNullOrWhiteSpace([string]$logConfig.logType)) {
                throw "$Context audit log config type is empty"
            }
            $canonicalLog = [ordered]@{ logType = [string]$logConfig.logType }
            if ('exemptedMembers' -in $properties) {
                $members = @(
                    $logConfig.exemptedMembers |
                    ForEach-Object { [string]$_ } |
                    Sort-Object
                )
                if (@($members | Select-Object -Unique).Count -ne $members.Count) {
                    throw "$Context audit log config contains duplicate exempted members"
                }
                $canonicalLog.exemptedMembers = $members
            }
            $canonicalLogObject = [pscustomobject]$canonicalLog
            $logEntries += [pscustomobject]@{
                Json = ($canonicalLogObject | ConvertTo-Json -Depth 20 -Compress)
                Value = $canonicalLogObject
            }
        }
        $canonicalLogs = @(
            $logEntries | Sort-Object Json | ForEach-Object { $_.Value }
        )
        $canonicalConfig = [pscustomobject]([ordered]@{
                service = [string]$auditConfig.service
                auditLogConfigs = $canonicalLogs
            })
        $configEntries += [pscustomobject]@{
            Json = ($canonicalConfig | ConvertTo-Json -Depth 20 -Compress)
            Value = $canonicalConfig
        }
    }
    $configJson = @($configEntries.Json)
    if (@($configJson | Select-Object -Unique).Count -ne $configJson.Count) {
        throw "$Context contains duplicate audit configs"
    }
    return @($configEntries | Sort-Object Json | ForEach-Object { $_.Value })
}

function Get-CleanupProjectPolicyEvidence {
    param(
        [Parameter(Mandatory)]
        [object]$Policy,

        [Parameter(Mandatory)]
        [string]$Context
    )

    $properties = @($Policy.PSObject.Properties.Name | Sort-Object)
    $allowedProperties = @('auditConfigs', 'bindings', 'etag', 'version')
    foreach ($propertyName in $properties) {
        if ($propertyName -notin $allowedProperties) {
            throw "$Context contains unsupported top-level field '$propertyName'"
        }
    }
    foreach ($requiredProperty in @('bindings', 'etag', 'version')) {
        if ($requiredProperty -notin $properties) {
            throw "$Context is missing '$requiredProperty'"
        }
    }
    if ([string]::IsNullOrWhiteSpace([string]$Policy.etag)) {
        throw "$Context etag is empty"
    }
    Assert-NoWithcondPlaceholder -Policy $Policy
    $version = [int]$Policy.version
    $hasConditions = @(
        @($Policy.bindings) | Where-Object {
            'condition' -in @($_.PSObject.Properties.Name)
        }
    ).Count -gt 0
    if ($hasConditions -and $version -ne 3) {
        throw "$Context must be condition-capable policy version 3"
    }
    if (-not $hasConditions -and $version -notin @(1, 3)) {
        throw "$Context has unsupported IAM policy version '$version'"
    }
    $semanticVersion = if ($hasConditions) { 3 } else { 1 }
    $bindingPolicy = [pscustomobject]@{
        bindings = @($Policy.bindings)
        version = $semanticVersion
    }
    $bindingEvidence = Get-IamPolicyEvidence `
        -Policy $bindingPolicy `
        -Context $Context
    $canonical = [ordered]@{
        version = $semanticVersion
        bindings = $bindingEvidence.Canonical.bindings
    }
    if ('auditConfigs' -in $properties) {
        $canonical.auditConfigs = ConvertTo-CanonicalAuditConfigs `
            -AuditConfigs @($Policy.auditConfigs) `
            -Context $Context
    }
    $canonicalObject = [pscustomobject]$canonical
    return [pscustomobject]@{
        Canonical = $canonicalObject
        Json = ($canonicalObject | ConvertTo-Json -Depth 100 -Compress)
    }
}

function Test-CleanupProjectPolicyMatchesAccepted {
    param(
        [Parameter(Mandatory)]
        [object]$LivePolicy,

        [Parameter(Mandatory)]
        [object]$AcceptedRecord
    )

    try {
        $acceptedPolicy = [pscustomobject]([ordered]@{
                bindings = @($AcceptedRecord.policy.bindings)
                etag = 'accepted-contract'
                version = [int]$AcceptedRecord.policy.version
            })
        $liveEvidence = Get-CleanupProjectPolicyEvidence `
            -Policy $LivePolicy `
            -Context 'live project IAM after temporary cleanup'
        $acceptedEvidence = Get-CleanupProjectPolicyEvidence `
            -Policy $acceptedPolicy `
            -Context 'accepted project IAM after temporary cleanup'
        return $liveEvidence.Json -ceq $acceptedEvidence.Json
    }
    catch {
        return $false
    }
}

function Get-ExactTemporaryBindingCount {
    param(
        [Parameter(Mandatory)]
        [object]$Policy
    )

    return @(
        @($Policy.bindings) | Where-Object { Test-ExactTemporaryBinding -Binding $_ }
    ).Count
}

function New-TemporaryProjectPolicy {
    param(
        [Parameter(Mandatory)]
        [object]$AcceptedProjectPolicy,

        [Parameter(Mandatory)]
        [string]$Etag
    )

    return [pscustomobject]([ordered]@{
            bindings = @($AcceptedProjectPolicy.bindings) + @(
                New-TemporaryBinding
            )
            etag = $Etag
            version = 3
        })
}

function Get-ProjectPolicyState {
    param(
        [Parameter(Mandatory)]
        [object]$Policy,

        [Parameter(Mandatory)]
        [object]$Acceptance
    )

    Assert-NoWithcondPlaceholder -Policy $Policy
    $properties = @($Policy.PSObject.Properties.Name)
    if ('etag' -notin $properties -or [string]::IsNullOrWhiteSpace([string]$Policy.etag)) {
        throw 'live project IAM policy is missing a non-empty etag'
    }
    $evidence = Get-IamPolicyEvidence `
        -Policy $Policy `
        -Context 'live project IAM policy' `
        -AllowEtag
    if ($evidence.Json -ceq $Acceptance.project_policy.CanonicalJson) {
        return 'baseline'
    }
    $temporaryPolicy = New-TemporaryProjectPolicy `
        -AcceptedProjectPolicy $Acceptance.project_policy.policy `
        -Etag ([string]$Policy.etag)
    $temporaryEvidence = Get-IamPolicyEvidence `
        -Policy $temporaryPolicy `
        -Context 'expected temporary project IAM policy' `
        -AllowEtag
    if ($evidence.Json -ceq $temporaryEvidence.Json) {
        return 'temporary'
    }
    throw 'live project IAM is neither the exact accepted baseline nor baseline plus the approved temporary binding'
}

function New-ProjectPolicyWithoutTemporaryBinding {
    param(
        [Parameter(Mandatory)]
        [object]$CurrentPolicy
    )

    Assert-NoWithcondPlaceholder -Policy $CurrentPolicy
    $null = Get-CleanupProjectPolicyEvidence `
        -Policy $CurrentPolicy `
        -Context 'live project IAM before temporary cleanup'
    $temporaryCount = Get-ExactTemporaryBindingCount -Policy $CurrentPolicy
    if ($temporaryCount -ne 1) {
        throw "temporary cleanup requires exactly one exact approved binding; found $temporaryCount"
    }
    $preservedBindings = @(
        @($CurrentPolicy.bindings) |
        Where-Object { -not (Test-ExactTemporaryBinding -Binding $_) }
    )
    $cleanedPolicy = [ordered]@{}
    foreach ($property in $CurrentPolicy.PSObject.Properties) {
        if ($property.Name -ceq 'bindings') {
            $cleanedPolicy.bindings = $preservedBindings
        }
        else {
            $cleanedPolicy[$property.Name] = $property.Value
        }
    }
    return [pscustomobject]$cleanedPolicy
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
    if ($Bucket.versioning.enabled -ne $true) {
        throw 'bucket versioning is not enabled'
    }
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

function Test-BucketMetadataMatches {
    param(
        [Parameter(Mandatory)]
        [object]$Bucket
    )

    try {
        Assert-BucketMetadata -Bucket $Bucket
        return $true
    }
    catch {
        return $false
    }
}

function Assert-ArchiveBucketMetadata {
    param(
        [Parameter(Mandatory)]
        [object]$Bucket
    )

    Assert-ExactValue -Name 'archive bucket.name' -Actual $Bucket.name -Expected $ExpectedArchiveBucket
    Assert-ExactValue `
        -Name 'archive bucket.projectNumber' `
        -Actual $Bucket.projectNumber `
        -Expected $ExpectedProjectNumber
    Assert-ExactValue `
        -Name 'archive bucket.location' `
        -Actual $Bucket.location `
        -Expected $ExpectedLocation
    Assert-ExactValue `
        -Name 'archive bucket.storageClass' `
        -Actual $Bucket.storageClass `
        -Expected 'STANDARD'
    if ($Bucket.iamConfiguration.uniformBucketLevelAccess.enabled -ne $true) {
        throw 'archive bucket uniform bucket-level access is not enabled'
    }
    Assert-ExactValue `
        -Name 'archive bucket public access prevention' `
        -Actual $Bucket.iamConfiguration.publicAccessPrevention `
        -Expected 'enforced'
    Assert-ExactValue `
        -Name 'archive bucket soft delete seconds' `
        -Actual $Bucket.softDeletePolicy.retentionDurationSeconds `
        -Expected $ExpectedSoftDeleteSeconds
    if ($Bucket.versioning.enabled -ne $true) {
        throw 'archive bucket versioning is not enabled'
    }
    $labelsProperty = $Bucket.PSObject.Properties['labels']
    if ($null -eq $labelsProperty -or $null -eq $labelsProperty.Value) {
        throw 'archive bucket does not contain the Terraform provenance label'
    }
    $actualLabelNames = @($labelsProperty.Value.PSObject.Properties.Name | Sort-Object)
    $expectedLabelNames = @($ExpectedArchiveLabels.Keys | Sort-Object)
    if (@(Compare-Object $expectedLabelNames $actualLabelNames).Count -ne 0) {
        throw 'archive bucket Terraform provenance label set does not match'
    }
    foreach ($labelName in $ExpectedArchiveLabels.Keys) {
        Assert-ExactValue `
            -Name "archive bucket label $labelName" `
            -Actual $labelsProperty.Value.$labelName `
            -Expected $ExpectedArchiveLabels[$labelName]
    }
    $retentionPolicy = $Bucket.PSObject.Properties['retentionPolicy']
    if ($null -ne $retentionPolicy -and $null -ne $retentionPolicy.Value) {
        throw 'archive bucket has an unapproved retention policy'
    }
    $lifecycle = $Bucket.PSObject.Properties['lifecycle']
    if ($null -ne $lifecycle -and $null -ne $lifecycle.Value) {
        throw 'archive bucket has an unapproved lifecycle configuration'
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

function Get-BucketPolicyState {
    param(
        [Parameter(Mandatory)]
        [object]$Policy
    )

    $properties = @($Policy.PSObject.Properties.Name | Sort-Object)
    foreach ($propertyName in $properties) {
        if ($propertyName -notin @('bindings', 'etag', 'version')) {
            throw "bucket IAM contains unexpected top-level field '$propertyName'"
        }
    }
    foreach ($required in @('bindings', 'etag')) {
        if ($required -notin $properties) {
            throw "bucket IAM is missing required field '$required'"
        }
    }
    if ([string]::IsNullOrWhiteSpace([string]$Policy.etag)) {
        throw 'bucket IAM etag is empty'
    }
    if ('version' -in $properties -and [int]$Policy.version -ne 1) {
        throw 'bucket IAM version must be 1 when present'
    }
    $bindings = @($Policy.bindings)
    if (Test-IamBindingsExact -Bindings $bindings -Expected $ExpectedLockoutIamBindings) {
        return 'lockout'
    }
    if (Test-IamBindingsExact -Bindings $bindings -Expected $ExpectedDurableIamBindings) {
        return 'durable'
    }
    throw 'bucket direct IAM is neither the exact observed lockout policy nor the approved durable target'
}

function Assert-ArchiveBucketIam {
    param(
        [Parameter(Mandatory)]
        [object]$Policy
    )

    $properties = @($Policy.PSObject.Properties.Name | Sort-Object)
    foreach ($propertyName in $properties) {
        if ($propertyName -notin @('bindings', 'etag', 'version')) {
            throw "archive bucket IAM contains unexpected top-level field '$propertyName'"
        }
    }
    foreach ($required in @('bindings', 'etag')) {
        if ($required -notin $properties) {
            throw "archive bucket IAM is missing required field '$required'"
        }
    }
    if ([string]::IsNullOrWhiteSpace([string]$Policy.etag)) {
        throw 'archive bucket IAM etag is empty'
    }
    if ('version' -in $properties -and [int]$Policy.version -ne 1) {
        throw 'archive bucket IAM version must be 1 when present'
    }
    if (-not (Test-IamBindingsExact `
            -Bindings @($Policy.bindings) `
            -Expected $ExpectedArchiveIamBindings)) {
        throw 'archive bucket direct IAM is not the exact Terraform-managed writer-only policy'
    }
}

function Get-LiveProjectIam {
    $policy = Invoke-GcloudJson -Arguments @(
        'projects', 'get-iam-policy', $ExpectedProjectId,
        "--account=$ExpectedAccount", '--format=json'
    )
    Assert-NoWithcondPlaceholder -Policy $policy
    return $policy
}

function Get-LiveOrganizationIam {
    $policy = Invoke-GcloudJson -Arguments @(
        'organizations', 'get-iam-policy', $ExpectedOrganizationId,
        "--account=$ExpectedAccount", '--format=json'
    )
    Assert-NoWithcondPlaceholder -Policy $policy
    return $policy
}

function Get-LiveBucket {
    param([switch]$AllowExactBucketPermissionDenied)
    return Invoke-GcloudJson `
        -AllowExactBucketPermissionDenied:$AllowExactBucketPermissionDenied `
        -Arguments @(
            'storage', 'buckets', 'describe', $ExpectedBucketUri,
            "--project=$ExpectedProjectId", "--account=$ExpectedAccount",
            '--raw', '--format=json'
        )
}

function Get-LiveBucketIam {
    param([switch]$AllowExactBucketPermissionDenied)
    return Invoke-GcloudJson `
        -AllowExactBucketPermissionDenied:$AllowExactBucketPermissionDenied `
        -Arguments @(
            'storage', 'buckets', 'get-iam-policy', $ExpectedBucketUri,
            "--project=$ExpectedProjectId", "--account=$ExpectedAccount", '--format=json'
        )
}

function Get-LiveArchiveBucket {
    param([switch]$AllowExactBucketPermissionDenied)
    return Invoke-GcloudJson `
        -AllowExactBucketPermissionDenied:$AllowExactBucketPermissionDenied `
        -Arguments @(
            'storage', 'buckets', 'describe', $ExpectedArchiveBucketUri,
            "--project=$ExpectedProjectId", "--account=$ExpectedAccount",
            '--raw', '--format=json'
        )
}

function Get-LiveArchiveBucketIam {
    param([switch]$AllowExactBucketPermissionDenied)
    return Invoke-GcloudJson `
        -AllowExactBucketPermissionDenied:$AllowExactBucketPermissionDenied `
        -Arguments @(
            'storage', 'buckets', 'get-iam-policy', $ExpectedArchiveBucketUri,
            "--project=$ExpectedProjectId", "--account=$ExpectedAccount", '--format=json'
        )
}

function Wait-TemporaryBindingRemoved {
    param(
        [Parameter(Mandatory)]
        [object]$BeforePolicy,

        [Parameter(Mandatory)]
        [object]$ExpectedPolicy
    )

    $beforeEvidence = Get-CleanupProjectPolicyEvidence `
        -Policy $BeforePolicy `
        -Context 'project IAM before temporary cleanup'
    $expectedEvidence = Get-CleanupProjectPolicyEvidence `
        -Policy $ExpectedPolicy `
        -Context 'expected project IAM after temporary cleanup'
    for ($attempt = 1; $attempt -le $MaxProbeAttempts; $attempt++) {
        $policy = Get-LiveProjectIam
        $evidence = Get-CleanupProjectPolicyEvidence `
            -Policy $policy `
            -Context 'live project IAM after temporary cleanup'
        if ($evidence.Json -ceq $expectedEvidence.Json) {
            if ((Get-ExactTemporaryBindingCount -Policy $policy) -ne 0) {
                throw 'temporary binding remained in the verified cleanup target'
            }
            return $policy
        }
        if ($evidence.Json -cne $beforeEvidence.Json) {
            throw 'project IAM changed unexpectedly while waiting for temporary binding removal'
        }
        if ($attempt -lt $MaxProbeAttempts -and $ProbeIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $ProbeIntervalSeconds
        }
    }
    throw "temporary binding was not removed after $MaxProbeAttempts read-only probes"
}

function Wait-ProjectPolicyState {
    param(
        [Parameter(Mandatory)]
        [string]$TargetState,

        [Parameter(Mandatory)]
        [string]$TransitionalState,

        [Parameter(Mandatory)]
        [object]$Acceptance
    )

    for ($attempt = 1; $attempt -le $MaxProbeAttempts; $attempt++) {
        $policy = Get-LiveProjectIam
        $state = Get-ProjectPolicyState -Policy $policy -Acceptance $Acceptance
        if ($state -ceq $TargetState) {
            return $policy
        }
        if ($state -cne $TransitionalState) {
            throw "project IAM entered unexpected state '$state' while waiting for '$TargetState'"
        }
        if ($attempt -lt $MaxProbeAttempts -and $ProbeIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $ProbeIntervalSeconds
        }
    }
    throw "project IAM did not reach '$TargetState' after $MaxProbeAttempts read-only probes"
}

function Wait-ArchiveOperatorProjectPolicyState {
    param(
        [Parameter(Mandatory)]
        [object]$AcceptedPolicies
    )

    for ($attempt = 1; $attempt -le $MaxProbeAttempts; $attempt++) {
        $policy = Get-LiveProjectIam
        $state = Get-ArchiveOperatorProjectPolicyState `
            -Policy $policy `
            -AcceptedPolicies $AcceptedPolicies
        if ($state -ceq 'target') {
            return $policy
        }
        if ($state -cne 'baseline') {
            throw "project IAM entered unexpected state '$state' while waiting for the archive operator target"
        }
        if ($attempt -lt $MaxProbeAttempts -and $ProbeIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $ProbeIntervalSeconds
        }
    }
    throw "project IAM did not reach the archive operator target after $MaxProbeAttempts read-only probes"
}

function Wait-ArchiveAccess {
    for ($attempt = 1; $attempt -le $MaxProbeAttempts; $attempt++) {
        $bucket = Get-LiveArchiveBucket -AllowExactBucketPermissionDenied
        if ($null -ne $bucket) {
            Assert-ArchiveBucketMetadata -Bucket $bucket
            $policy = Get-LiveArchiveBucketIam -AllowExactBucketPermissionDenied
            if ($null -ne $policy) {
                Assert-ArchiveBucketIam -Policy $policy
                return [pscustomobject]@{ Bucket = $bucket; Policy = $policy }
            }
        }
        if ($attempt -lt $MaxProbeAttempts -and $ProbeIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $ProbeIntervalSeconds
        }
    }
    throw "archive bucket access was not restored after $MaxProbeAttempts read-only probes"
}

function Assert-ArchiveOperatorFinalPolicies {
    param(
        [Parameter(Mandatory)]
        [object]$Acceptance,

        [Parameter(Mandatory)]
        [object]$AcceptedPolicies
    )

    $finalOrganizationPolicy = Get-LiveOrganizationIam
    Assert-InheritedIamPolicyMatches `
        -Scope 'organization' `
        -LivePolicy $finalOrganizationPolicy `
        -AcceptedRecord $Acceptance.organization_policy
    $finalProjectPolicy = Get-LiveProjectIam
    $finalProjectState = Get-ArchiveOperatorProjectPolicyState `
        -Policy $finalProjectPolicy `
        -AcceptedPolicies $AcceptedPolicies
    if ($finalProjectState -cne 'target') {
        throw 'final project IAM is not the exact archive operator target'
    }
}

function Wait-BucketAccess {
    for ($attempt = 1; $attempt -le $MaxProbeAttempts; $attempt++) {
        $bucket = Get-LiveBucket -AllowExactBucketPermissionDenied
        if ($null -ne $bucket) {
            Assert-BucketMetadata -Bucket $bucket
            $policy = Get-LiveBucketIam -AllowExactBucketPermissionDenied
            if ($null -ne $policy) {
                return [pscustomobject]@{ Bucket = $bucket; Policy = $policy }
            }
        }
        if ($attempt -lt $MaxProbeAttempts -and $ProbeIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $ProbeIntervalSeconds
        }
    }
    throw "bucket access was not restored after $MaxProbeAttempts read-only probes"
}

function Wait-CleanupBucketDurableIam {
    for ($attempt = 1; $attempt -le $MaxProbeAttempts; $attempt++) {
        $policy = Get-LiveBucketIam -AllowExactBucketPermissionDenied
        if ($null -ne $policy) {
            if ((Get-BucketPolicyState -Policy $policy) -cne 'durable') {
                throw 'temporary project binding cannot be revoked before durable bucket IAM is verified'
            }
            return $policy
        }
        if ($attempt -lt $MaxProbeAttempts -and $ProbeIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $ProbeIntervalSeconds
        }
    }
    throw "durable bucket IAM was not visible after $MaxProbeAttempts read-only probes"
}

function Wait-BucketPolicyState {
    param(
        [Parameter(Mandatory)]
        [string]$TargetState,

        [Parameter(Mandatory)]
        [string]$TransitionalState
    )

    for ($attempt = 1; $attempt -le $MaxProbeAttempts; $attempt++) {
        $policy = Get-LiveBucketIam
        $state = Get-BucketPolicyState -Policy $policy
        if ($state -ceq $TargetState) {
            return $policy
        }
        if ($state -cne $TransitionalState) {
            throw "bucket IAM entered unexpected state '$state' while waiting for '$TargetState'"
        }
        if ($attempt -lt $MaxProbeAttempts -and $ProbeIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $ProbeIntervalSeconds
        }
    }
    throw "bucket IAM did not reach '$TargetState' after $MaxProbeAttempts read-only probes"
}

function Set-ProjectPolicyOnce {
    param(
        [Parameter(Mandatory)]
        [object]$Policy
    )

    $policyFile = New-TemporaryJsonFile -Value $Policy
    try {
        $null = Invoke-GcloudJson -Arguments @(
            'projects', 'set-iam-policy', $ExpectedProjectId, $policyFile.Path,
            "--account=$ExpectedAccount", '--quiet', '--format=json'
        )
    }
    finally {
        $policyFile.Stream.Dispose()
        Remove-TemporaryJsonFile -Path $policyFile.Path
    }
}

function Set-DurableBucketPolicyOnce {
    param(
        [Parameter(Mandatory)]
        [object]$CurrentPolicy
    )

    $policy = [pscustomobject]([ordered]@{
            bindings = @(
                [pscustomobject]([ordered]@{
                        role = 'roles/storage.legacyBucketOwner'
                        members = @("user:$ExpectedAccount")
                    }),
                [pscustomobject]([ordered]@{
                        role = 'roles/storage.objectAdmin'
                        members = @("user:$ExpectedAccount")
                    })
            )
            version = 1
        })
    $policyFile = New-TemporaryJsonFile -Value $policy
    try {
        $null = Invoke-GcloudJson -Arguments @(
            'storage', 'buckets', 'set-iam-policy', $ExpectedBucketUri, $policyFile.Path,
            "--project=$ExpectedProjectId", "--account=$ExpectedAccount",
            "--etag=$($CurrentPolicy.etag)", '--quiet', '--format=json'
        )
    }
    finally {
        $policyFile.Stream.Dispose()
        Remove-TemporaryJsonFile -Path $policyFile.Path
    }
}

function Write-Report {
    param(
        [Parameter(Mandatory)]
        [string]$Status,

        [Parameter(Mandatory)]
        [bool]$Mutated
    )

    $reportBucket = if ($Mode -in @('CheckArchiveOperator', 'GrantArchiveOperator')) {
        $ExpectedArchiveBucket
    }
    else {
        $ExpectedBucket
    }
    [pscustomobject]@{
        bucket = $reportBucket
        mode = $Mode
        mutated = $Mutated
        status = $Status
    } | ConvertTo-Json -Compress
}

$script:ResolvedGcloudCommand = Get-Command -Name $GcloudExecutable -ErrorAction SilentlyContinue
if ($null -eq $script:ResolvedGcloudCommand) {
    throw "gcloud executable not found: $GcloudExecutable"
}
Assert-ConditionCapableGcloudVersion

$resolvedPreflight = (Resolve-Path -LiteralPath $PreflightPath -ErrorAction Stop).Path
$preflight = Get-Content -Raw -LiteralPath $resolvedPreflight | ConvertFrom-Json -Depth 100
Assert-ExactValue -Name 'preflight.active_account' -Actual $preflight.active_account -Expected $ExpectedAccount
Assert-ExactValue -Name 'preflight.active_project' -Actual $preflight.active_project -Expected $ExpectedProjectId
Assert-ExactValue -Name 'preflight.project_number' -Actual $preflight.project_number -Expected $ExpectedProjectNumber
Assert-ExactValue -Name 'preflight.project_lifecycle' -Actual $preflight.project_lifecycle -Expected 'ACTIVE'
if ($preflight.billing_enabled -ne $true -or $preflight.ready_for_archive -ne $true -or `
        @($preflight.blockers).Count -ne 0) {
    throw 'preflight is not ready for recovery'
}
$acceptance = Get-InheritedIamAcceptance
$archiveAcceptedPolicies = $null
if ($Mode -in @('CheckArchiveOperator', 'GrantArchiveOperator')) {
    $archiveAcceptedPolicies = Get-ArchiveOperatorAcceptedPolicies -Acceptance $acceptance
}

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
    'projects', 'describe', $ExpectedProjectId,
    "--account=$ExpectedAccount", '--format=json'
)
Assert-ExactValue -Name 'live project ID' -Actual $project.projectId -Expected $ExpectedProjectId
Assert-ExactValue -Name 'live project number' -Actual $project.projectNumber -Expected $ExpectedProjectNumber
Assert-ExactValue -Name 'live project lifecycle' -Actual $project.lifecycleState -Expected 'ACTIVE'
Assert-ExactValue -Name 'live project parent type' -Actual $project.parent.type -Expected 'organization'
Assert-ExactValue -Name 'live project parent organization' -Actual $project.parent.id -Expected $ExpectedOrganizationId
$billing = Invoke-GcloudJson -Arguments @(
    'billing', 'projects', 'describe', $ExpectedProjectId,
    "--account=$ExpectedAccount", '--format=json'
)
Assert-ExactValue -Name 'billing project ID' -Actual $billing.projectId -Expected $ExpectedProjectId
if ($billing.billingEnabled -ne $true) {
    throw 'live project billing is not enabled'
}

if ($Mode -in @('CheckArchiveOperator', 'GrantArchiveOperator')) {
    $organizationPolicy = Get-LiveOrganizationIam
    Assert-InheritedIamPolicyMatches `
        -Scope 'organization' `
        -LivePolicy $organizationPolicy `
        -AcceptedRecord $acceptance.organization_policy

    $projectPolicy = Get-LiveProjectIam
    $projectPolicyState = Get-ArchiveOperatorProjectPolicyState `
        -Policy $projectPolicy `
        -AcceptedPolicies $archiveAcceptedPolicies

    if ($projectPolicyState -ceq 'target') {
        $null = Wait-ArchiveAccess
        Assert-ArchiveOperatorFinalPolicies `
            -Acceptance $acceptance `
            -AcceptedPolicies $archiveAcceptedPolicies
        Write-Report -Status 'archive_operator_ready' -Mutated $false
        exit 0
    }

    $authorizationProbe = Get-LiveArchiveBucket -AllowExactBucketPermissionDenied
    if ($null -ne $authorizationProbe) {
        throw 'archive operator grant requires a fresh exact storage.buckets.get denial while the project IAM is the approved pre-grant baseline'
    }
    if ($Mode -ceq 'CheckArchiveOperator') {
        Write-Report -Status 'requires_archive_operator_grant' -Mutated $false
        exit 0
    }

    $latestProjectPolicy = Get-LiveProjectIam
    $latestProjectPolicyState = Get-ArchiveOperatorProjectPolicyState `
        -Policy $latestProjectPolicy `
        -AcceptedPolicies $archiveAcceptedPolicies
    if ($latestProjectPolicyState -cne 'baseline') {
        throw 'project IAM changed before the archive operator write; refusing mutation'
    }
    $targetProjectPolicy = New-ArchiveOperatorProjectPolicy `
        -AcceptedPolicies $archiveAcceptedPolicies `
        -Etag ([string]$latestProjectPolicy.etag)
    Set-ProjectPolicyOnce -Policy $targetProjectPolicy
    $null = Wait-ArchiveOperatorProjectPolicyState `
        -AcceptedPolicies $archiveAcceptedPolicies
    $null = Wait-ArchiveAccess
    Assert-ArchiveOperatorFinalPolicies `
        -Acceptance $acceptance `
        -AcceptedPolicies $archiveAcceptedPolicies
    Write-Report -Status 'archive_operator_granted' -Mutated $true
    exit 0
}

if ($Mode -ceq 'RevokeTemporary') {
    $null = Wait-CleanupBucketDurableIam
    $projectPolicy = Get-LiveProjectIam
    $temporaryCount = Get-ExactTemporaryBindingCount -Policy $projectPolicy
    if ($temporaryCount -gt 1) {
        throw "temporary cleanup requires at most one exact approved binding; found $temporaryCount"
    }

    $mutated = $false
    if ($temporaryCount -eq 1) {
        $cleanedPolicy = New-ProjectPolicyWithoutTemporaryBinding -CurrentPolicy $projectPolicy
        Set-ProjectPolicyOnce -Policy $cleanedPolicy
        $projectPolicy = Wait-TemporaryBindingRemoved `
            -BeforePolicy $projectPolicy `
            -ExpectedPolicy $cleanedPolicy
        $mutated = $true
    }

    $finalOrganizationPolicy = Get-LiveOrganizationIam
    $finalBucket = Get-LiveBucket
    $metadataAccepted = Test-BucketMetadataMatches -Bucket $finalBucket
    $finalBucketPolicy = Get-LiveBucketIam
    if ((Get-BucketPolicyState -Policy $finalBucketPolicy) -cne 'durable') {
        throw 'final bucket IAM is not the approved durable target'
    }

    $projectAccepted = Test-CleanupProjectPolicyMatchesAccepted `
        -LivePolicy $projectPolicy `
        -AcceptedRecord $acceptance.project_policy
    $organizationAccepted = Test-InheritedIamPolicyMatches `
        -LivePolicy $finalOrganizationPolicy `
        -AcceptedRecord $acceptance.organization_policy
    if ($projectAccepted -and $organizationAccepted -and $metadataAccepted) {
        Write-Report -Status 'recovery_complete' -Mutated $mutated
        exit 0
    }
    Write-Report `
        -Status 'temporary_removed_drift_requires_escalation' `
        -Mutated $mutated
    exit 2
}

$projectPolicy = Get-LiveProjectIam
$projectPolicyState = Get-ProjectPolicyState -Policy $projectPolicy -Acceptance $acceptance
$organizationPolicy = Get-LiveOrganizationIam
Assert-InheritedIamPolicyMatches `
    -Scope 'organization' `
    -LivePolicy $organizationPolicy `
    -AcceptedRecord $acceptance.organization_policy

switch ($Mode) {
    'Check' {
        if ($projectPolicyState -ceq 'baseline') {
            $bucket = Get-LiveBucket -AllowExactBucketPermissionDenied
            if ($null -eq $bucket) {
                Write-Report -Status 'requires_temporary_grant' -Mutated $false
                exit 0
            }
            Assert-BucketMetadata -Bucket $bucket
            $bucketPolicy = Get-LiveBucketIam
            if ((Get-BucketPolicyState -Policy $bucketPolicy) -cne 'durable') {
                throw 'baseline project IAM can access a backend whose direct IAM is not durable'
            }
            Write-Report -Status 'recovery_complete' -Mutated $false
            exit 0
        }
        $bucketAccess = Wait-BucketAccess
        $bucketState = Get-BucketPolicyState -Policy $bucketAccess.Policy
        if ($bucketState -ceq 'lockout') {
            Write-Report -Status 'ready_to_repair_bucket' -Mutated $false
        }
        else {
            Write-Report -Status 'ready_to_revoke_temporary' -Mutated $false
        }
        exit 0
    }
    'GrantTemporary' {
        if ($projectPolicyState -ceq 'baseline') {
            $authorizationProbe = Get-LiveBucket -AllowExactBucketPermissionDenied
            if ($null -ne $authorizationProbe) {
                throw 'GrantTemporary requires a fresh exact storage.buckets.get denial immediately before mutation'
            }
            $temporaryPolicy = New-TemporaryProjectPolicy `
                -AcceptedProjectPolicy $acceptance.project_policy.policy `
                -Etag ([string]$projectPolicy.etag)
            Set-ProjectPolicyOnce -Policy $temporaryPolicy
            $projectPolicy = Wait-ProjectPolicyState `
                -TargetState 'temporary' `
                -TransitionalState 'baseline' `
                -Acceptance $acceptance
        }
        $bucketAccess = Wait-BucketAccess
        $bucketState = Get-BucketPolicyState -Policy $bucketAccess.Policy
        if ($bucketState -ceq 'lockout') {
            Write-Report -Status 'temporary_granted' -Mutated ($projectPolicyState -ceq 'baseline')
        }
        else {
            Write-Report -Status 'ready_to_revoke_temporary' -Mutated ($projectPolicyState -ceq 'baseline')
        }
        exit 0
    }
    'RepairBucket' {
        if ($projectPolicyState -cne 'temporary') {
            throw 'RepairBucket requires the exact approved temporary project binding'
        }
        $bucketAccess = Wait-BucketAccess
        $bucketState = Get-BucketPolicyState -Policy $bucketAccess.Policy
        if ($bucketState -ceq 'durable') {
            Write-Report -Status 'bucket_repaired' -Mutated $false
            exit 0
        }
        Set-DurableBucketPolicyOnce -CurrentPolicy $bucketAccess.Policy
        $null = Wait-BucketPolicyState -TargetState 'durable' -TransitionalState 'lockout'
        Write-Report -Status 'bucket_repaired' -Mutated $true
        exit 0
    }
}
