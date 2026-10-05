#Requires -Version 7.0
[CmdletBinding(DefaultParameterSetName = 'Plan')]
param(
    [Parameter()]
    [ValidateSet('motionexpaiweb')]
    [string]$ProjectId = 'motionexpaiweb',

    [Parameter()]
    [ValidateSet('us-central1')]
    [string]$Region = 'us-central1',

    [Parameter()]
    [ValidateRange(1, 3600)]
    [int]$ProcessTimeoutSeconds = 1800,

    [Parameter(Mandatory = $true, ParameterSetName = 'Plan')]
    [switch]$PlanOnly,

    [Parameter(Mandatory = $true, ParameterSetName = 'Apply')]
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$projectNumber = '880586285913'
$instance = 'hk-movie-rag-pg'
$database = 'hk_movie_rag'
$databaseUser = 'hk_rag_app'
$bucket = "$ProjectId-$projectNumber-hk-movie-rag-release"
$repository = 'hk-movie-rag'
$serviceAccountName = 'hk-rag-runtime'
$serviceAccount = "$serviceAccountName@$ProjectId.iam.gserviceaccount.com"
$dbSecret = 'hk-rag-db-password'
$demoSecret = 'hk-rag-demo-key'
$job = 'hk-movie-rag-ingest'
$posterVerifyJob = 'hk-movie-rag-poster-verify'
$service = 'hk-movie-rag-demo'
$apis = @(
    'run.googleapis.com',
    'cloudbuild.googleapis.com',
    'artifactregistry.googleapis.com',
    'secretmanager.googleapis.com',
    'sqladmin.googleapis.com',
    'sql-component.googleapis.com',
    'aiplatform.googleapis.com',
    'storage.googleapis.com'
)

$commands = @(
    "gcloud services enable $($apis -join ' ') --project=$ProjectId",
    "gcloud iam service-accounts create $serviceAccountName --project=$ProjectId",
    "gcloud artifacts repositories create $repository --repository-format=docker --location=$Region --project=$ProjectId",
    "gcloud storage buckets create gs://$bucket --location=$Region --uniform-bucket-level-access --public-access-prevention --project=$ProjectId",
    "gcloud sql instances create $instance --database-version=POSTGRES_16 --edition=ENTERPRISE --tier=db-g1-small --storage-size=10 --storage-type=SSD --availability-type=zonal --no-storage-auto-increase --deletion-protection --region=$Region --project=$ProjectId",
    "gcloud secrets create $dbSecret --replication-policy=automatic --project=$ProjectId",
    "Secret Manager v1 versions.list/access/addVersion/disable over HTTPS for $dbSecret --project=$ProjectId",
    "gcloud secrets create $demoSecret --replication-policy=automatic --project=$ProjectId",
    "Secret Manager v1 versions.list/access/addVersion/disable over HTTPS for $demoSecret --project=$ProjectId",
    "gcloud sql databases create $database --instance=$instance --project=$ProjectId",
    "gcloud auth print-access-token --project=$ProjectId",
    "Cloud SQL Admin v1beta4 users.insert/users.update over HTTPS for $databaseUser --project=$ProjectId",
    "gcloud projects add-iam-policy-binding $ProjectId --member=serviceAccount:$serviceAccount --role=roles/aiplatform.user --project=$ProjectId",
    "gcloud projects add-iam-policy-binding $ProjectId --member=serviceAccount:$serviceAccount --role=roles/cloudsql.client --project=$ProjectId",
    "gcloud storage buckets add-iam-policy-binding gs://$bucket --member=serviceAccount:$serviceAccount --role=roles/storage.objectViewer --project=$ProjectId",
    "gcloud secrets add-iam-policy-binding $dbSecret --member=serviceAccount:$serviceAccount --role=roles/secretmanager.secretAccessor --project=$ProjectId",
    "gcloud secrets add-iam-policy-binding $demoSecret --member=serviceAccount:$serviceAccount --role=roles/secretmanager.secretAccessor --project=$ProjectId"
)

function New-ProvisionPlan {
    param([string]$Mode)
    [ordered]@{
        schema_version = 'rag-demo-provision/v1'
        mode = $Mode
        project_id = $ProjectId
        region = $Region
        apis = $apis
        cloud_sql_instance = $instance
        cloud_sql = [ordered]@{
            database_version = 'POSTGRES_16'
            edition = 'ENTERPRISE'
            tier = 'db-g1-small'
            storage_gb = 10
            storage_type = 'SSD'
            availability_type = 'ZONAL'
            storage_auto_resize = $false
            deletion_protection = $true
        }
        database = $database
        database_user = $databaseUser
        release_bucket = $bucket
        artifact_registry = $repository
        runtime_service_account = $serviceAccount
        secrets = @($dbSecret, $demoSecret)
        cloud_run_job = $job
        poster_verify_job = $posterVerifyJob
        cloud_run_service = $service
        commands = $commands
    }
}

if ($PlanOnly) {
    New-ProvisionPlan -Mode 'plan' | ConvertTo-Json -Depth 8 -Compress
    exit 0
}

$gcloudCommand = Get-Command gcloud -ErrorAction Stop
$script:Gcloud = $gcloudCommand.Source

function Get-SqlAdminApiBaseUri {
    $productionUri = [System.Uri]::new('https://sqladmin.googleapis.com')
    $testUriValue = $env:RAG_DEMO_TEST_SQL_ADMIN_API_BASE_URI
    if ([string]::IsNullOrWhiteSpace($testUriValue)) {
        return $productionUri
    }
    if ($env:RAG_DEMO_ALLOW_TEST_SQL_ADMIN_API -cne '1') {
        throw 'test Cloud SQL Admin API endpoint requires explicit opt-in'
    }
    if ([string]::IsNullOrWhiteSpace($env:PYTEST_CURRENT_TEST)) {
        throw 'test Cloud SQL Admin API endpoint requires active pytest context'
    }
    try {
        $testUri = [System.Uri]::new($testUriValue)
    }
    catch {
        throw 'test Cloud SQL Admin API endpoint is invalid'
    }
    if (
        -not $testUri.IsAbsoluteUri -or
        $testUri.Scheme -cne 'http' -or
        -not $testUri.IsLoopback -or
        -not [string]::IsNullOrEmpty($testUri.UserInfo) -or
        -not [string]::IsNullOrEmpty($testUri.Query) -or
        -not [string]::IsNullOrEmpty($testUri.Fragment) -or
        $testUri.AbsolutePath -ne '/'
    ) {
        throw 'test Cloud SQL Admin API endpoint must be an unqualified HTTP loopback origin'
    }
    return $testUri
}

$script:SqlAdminApiBaseUri = (Get-SqlAdminApiBaseUri).AbsoluteUri.TrimEnd('/')

function Get-SecretManagerApiBaseUri {
    $productionUri = [System.Uri]::new('https://secretmanager.googleapis.com')
    $testUriValue = $env:RAG_DEMO_TEST_SECRET_MANAGER_API_BASE_URI
    if ([string]::IsNullOrWhiteSpace($testUriValue)) {
        return $productionUri
    }
    if ($env:RAG_DEMO_ALLOW_TEST_SECRET_MANAGER_API -cne '1') {
        throw 'test Secret Manager API endpoint requires explicit opt-in'
    }
    if ([string]::IsNullOrWhiteSpace($env:PYTEST_CURRENT_TEST)) {
        throw 'test Secret Manager API endpoint requires active pytest context'
    }
    try {
        $testUri = [System.Uri]::new($testUriValue)
    }
    catch {
        throw 'test Secret Manager API endpoint is invalid'
    }
    if (
        -not $testUri.IsAbsoluteUri -or
        $testUri.Scheme -cne 'http' -or
        -not $testUri.IsLoopback -or
        -not [string]::IsNullOrEmpty($testUri.UserInfo) -or
        -not [string]::IsNullOrEmpty($testUri.Query) -or
        -not [string]::IsNullOrEmpty($testUri.Fragment) -or
        $testUri.AbsolutePath -ne '/'
    ) {
        throw 'test Secret Manager API endpoint must be an unqualified HTTP loopback origin'
    }
    return $testUri
}

$script:SecretManagerApiBaseUri = (Get-SecretManagerApiBaseUri).AbsoluteUri.TrimEnd('/')

function New-GcloudStartInfo {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    foreach ($ambientGoogleVariable in @(
        'GOOGLE_API_KEY', 'GOOGLE_CLOUD_LOCATION', 'GOOGLE_CLOUD_PROJECT'
    )) {
        $null = $startInfo.Environment.Remove($ambientGoogleVariable)
    }
    if ([System.IO.Path]::GetExtension($script:Gcloud) -eq '.ps1') {
        $startInfo.FileName = (Get-Process -Id $PID).Path
        foreach ($hostArgument in @(
            '-NoLogo', '-NoProfile', '-NonInteractive', '-File', $script:Gcloud
        )) {
            $startInfo.ArgumentList.Add($hostArgument)
        }
    }
    else {
        $startInfo.FileName = $script:Gcloud
    }
    foreach ($gcloudArgument in $Arguments) {
        $startInfo.ArgumentList.Add($gcloudArgument)
    }
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    return $startInfo
}

function Stop-GcloudProcessTree {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)
    if ($IsWindows -and -not $Process.HasExited) {
        $taskkillStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $taskkillStartInfo.FileName = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        foreach ($taskkillArgument in @('/PID', [string]$Process.Id, '/T', '/F')) {
            $taskkillStartInfo.ArgumentList.Add($taskkillArgument)
        }
        $taskkillStartInfo.UseShellExecute = $false
        $taskkillStartInfo.RedirectStandardOutput = $true
        $taskkillStartInfo.RedirectStandardError = $true
        $taskkillStartInfo.CreateNoWindow = $true
        $taskkillProcess = [System.Diagnostics.Process]::new()
        $taskkillProcess.StartInfo = $taskkillStartInfo
        try {
            if ($taskkillProcess.Start()) {
                $taskkillOutput = $taskkillProcess.StandardOutput.ReadToEndAsync()
                $taskkillError = $taskkillProcess.StandardError.ReadToEndAsync()
                if (-not $taskkillProcess.WaitForExit(5000)) {
                    $taskkillProcess.Kill($true)
                    $null = $taskkillProcess.WaitForExit(5000)
                }
                $null = $taskkillOutput.GetAwaiter().GetResult()
                $null = $taskkillError.GetAwaiter().GetResult()
            }
        }
        catch {
            # The managed tree-kill fallback below still targets only the process we started.
        }
        finally {
            $taskkillOutput = $null
            $taskkillError = $null
            $taskkillProcess.Dispose()
        }
    }
    try {
        if (-not $Process.HasExited) {
            $Process.Kill($true)
        }
    }
    catch {
        # Process exit can race either tree-kill mechanism.
    }
    $null = $Process.WaitForExit(5000)
}

function Wait-GcloudProcess {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    if ($Process.WaitForExit($TimeoutSeconds * 1000)) {
        return
    }
    Stop-GcloudProcessTree -Process $Process
    throw "gcloud command timed out after $TimeoutSeconds seconds without exposing command output"
}

function Invoke-Gcloud {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$AllowFailure
    )
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = New-GcloudStartInfo -Arguments $Arguments
    try {
        if (-not $process.Start()) {
            throw 'gcloud process did not start'
        }
        $standardOutput = $process.StandardOutput.ReadToEndAsync()
        $standardError = $process.StandardError.ReadToEndAsync()
        Wait-GcloudProcess -Process $process -TimeoutSeconds $ProcessTimeoutSeconds
        $output = $standardOutput.GetAwaiter().GetResult()
        $errorOutput = $standardError.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
    }
    finally {
        $standardOutput = $null
        $standardError = $null
        $errorOutput = $null
        $process.Dispose()
    }
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "gcloud command failed without exposing command output (exit $exitCode)"
    }
    [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

function Invoke-GcloudJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $result = Invoke-Gcloud -Arguments ($Arguments + '--format=json')
    if ([string]::IsNullOrWhiteSpace($result.Output)) {
        return @()
    }
    return ($result.Output | ConvertFrom-Json)
}

function Invoke-SqlAdminRequest {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('GET', 'POST', 'PUT')][string]$Method,
        [Parameter(Mandatory = $true)][System.Uri]$Uri,
        [Parameter(Mandatory = $true)][string]$AccessToken,
        [Parameter()][System.Collections.IDictionary]$Body,
        [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds
    )
    $client = [System.Net.Http.HttpClient]::new()
    $client.Timeout = [System.TimeSpan]::FromSeconds($TimeoutSeconds)
    $request = [System.Net.Http.HttpRequestMessage]::new(
        [System.Net.Http.HttpMethod]::new($Method),
        $Uri
    )
    $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new(
        'Bearer', $AccessToken
    )
    $jsonBody = $null
    if ($null -ne $Body) {
        $jsonBody = $Body | ConvertTo-Json -Depth 4 -Compress
        $request.Content = [System.Net.Http.StringContent]::new(
            $jsonBody,
            [System.Text.Encoding]::UTF8,
            'application/json'
        )
    }
    try {
        try {
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
        }
        catch {
            throw 'Cloud SQL Admin API request failed without exposing request or response content'
        }
        try {
            if (-not $response.IsSuccessStatusCode) {
                $statusCode = [int]$response.StatusCode
                throw "Cloud SQL Admin API request failed without exposing request or response content (HTTP $statusCode)"
            }
            try {
                $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                $parsed = $responseText | ConvertFrom-Json
            }
            catch {
                throw 'Cloud SQL Admin API returned an invalid response without exposing its content'
            }
            if ($null -eq $parsed) {
                throw 'Cloud SQL Admin API returned an invalid response without exposing its content'
            }
            return $parsed
        }
        finally {
            $responseText = $null
            $response.Dispose()
        }
    }
    finally {
        $jsonBody = $null
        $request.Dispose()
        $client.Dispose()
    }
}

function Wait-SqlAdminOperation {
    param(
        [Parameter(Mandatory = $true)][object]$Operation,
        [Parameter(Mandatory = $true)][string]$AccessToken
    )
    $operationName = [string]$Operation.name
    if ([string]::IsNullOrWhiteSpace($operationName)) {
        throw 'Cloud SQL Admin API returned an operation without a usable name'
    }
    $deadline = [System.DateTimeOffset]::UtcNow.AddSeconds($ProcessTimeoutSeconds)
    $projectSegment = [System.Uri]::EscapeDataString($ProjectId)
    $operationSegment = [System.Uri]::EscapeDataString($operationName)
    $operationUri = [System.Uri]::new(
        "$script:SqlAdminApiBaseUri/sql/v1beta4/projects/$projectSegment/operations/$operationSegment"
    )
    while ([string]$Operation.status -ne 'DONE') {
        $remaining = $deadline - [System.DateTimeOffset]::UtcNow
        if ($remaining.TotalSeconds -le 0) {
            throw "Cloud SQL Admin API operation timed out after $ProcessTimeoutSeconds seconds"
        }
        if ([string]$Operation.status -notin @('PENDING', 'RUNNING')) {
            throw 'Cloud SQL Admin API operation returned an invalid status'
        }
        Start-Sleep -Milliseconds ([Math]::Min(1000, [int]$remaining.TotalMilliseconds))
        $remaining = $deadline - [System.DateTimeOffset]::UtcNow
        if ($remaining.TotalSeconds -le 0) {
            throw "Cloud SQL Admin API operation timed out after $ProcessTimeoutSeconds seconds"
        }
        $requestTimeout = [Math]::Max(1, [Math]::Min(
            $ProcessTimeoutSeconds,
            [int][Math]::Ceiling($remaining.TotalSeconds)
        ))
        $Operation = Invoke-SqlAdminRequest -Method 'GET' -Uri $operationUri `
            -AccessToken $AccessToken -TimeoutSeconds $requestTimeout
    }
    if (
        $null -ne $Operation.PSObject.Properties['error'] -and
        $null -ne $Operation.error -and
        @($Operation.error.errors).Count -gt 0
    ) {
        throw 'Cloud SQL Admin API operation failed without exposing error details'
    }
}

function Set-CloudSqlBuiltInUserPassword {
    param(
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][bool]$UserExists
    )
    $tokenResult = Invoke-Gcloud -Arguments @(
        'auth', 'print-access-token', "--project=$ProjectId"
    )
    $accessToken = $tokenResult.Output.Trim()
    if ([string]::IsNullOrWhiteSpace($accessToken)) {
        throw 'gcloud auth returned no usable access token'
    }
    $projectSegment = [System.Uri]::EscapeDataString($ProjectId)
    $instanceSegment = [System.Uri]::EscapeDataString($instance)
    $userSegment = [System.Uri]::EscapeDataString($databaseUser)
    $userUriValue = "$script:SqlAdminApiBaseUri/sql/v1beta4/projects/$projectSegment/instances/$instanceSegment/users"
    $method = 'POST'
    if ($UserExists) {
        $method = 'PUT'
        $userUriValue = "$userUriValue`?name=$userSegment"
    }
    $body = [ordered]@{
        name = $databaseUser
        password = $Password
        type = 'BUILT_IN'
    }
    try {
        $operation = Invoke-SqlAdminRequest -Method $method -Uri ([System.Uri]::new($userUriValue)) `
            -AccessToken $accessToken -Body $body -TimeoutSeconds $ProcessTimeoutSeconds
        Wait-SqlAdminOperation -Operation $operation -AccessToken $accessToken
    }
    finally {
        $body = $null
        $operation = $null
        $accessToken = $null
        $tokenResult = $null
    }
}

function Invoke-SecretManagerRequest {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory = $true)][System.Uri]$Uri,
        [Parameter(Mandatory = $true)][string]$AccessToken,
        [Parameter()][AllowNull()][System.Collections.IDictionary]$Body,
        [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds
    )
    $client = [System.Net.Http.HttpClient]::new()
    $client.Timeout = [System.TimeSpan]::FromSeconds($TimeoutSeconds)
    $request = [System.Net.Http.HttpRequestMessage]::new(
        [System.Net.Http.HttpMethod]::new($Method),
        $Uri
    )
    $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new(
        'Bearer', $AccessToken
    )
    $jsonBody = $null
    if ($null -ne $Body) {
        $jsonBody = $Body | ConvertTo-Json -Depth 5 -Compress
        $request.Content = [System.Net.Http.StringContent]::new(
            $jsonBody,
            [System.Text.Encoding]::UTF8,
            'application/json'
        )
    }
    try {
        try {
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
        }
        catch {
            throw 'Secret Manager API request failed without exposing request or response content'
        }
        try {
            if (-not $response.IsSuccessStatusCode) {
                $statusCode = [int]$response.StatusCode
                throw "Secret Manager API request failed without exposing request or response content (HTTP $statusCode)"
            }
            try {
                $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                $parsed = $responseText | ConvertFrom-Json
            }
            catch {
                throw 'Secret Manager API returned an invalid response without exposing its content'
            }
            if ($null -eq $parsed) {
                throw 'Secret Manager API returned an invalid response without exposing its content'
            }
            return $parsed
        }
        finally {
            $responseText = $null
            $response.Dispose()
        }
    }
    finally {
        $jsonBody = $null
        $request.Dispose()
        $client.Dispose()
    }
}

function New-RandomSecret {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Get-GcloudAccessToken {
    $tokenResult = Invoke-Gcloud -Arguments @(
        'auth', 'print-access-token', "--project=$ProjectId"
    )
    try {
        $accessToken = $tokenResult.Output.Trim()
        if ([string]::IsNullOrWhiteSpace($accessToken)) {
            throw 'gcloud auth returned no usable access token'
        }
        return $accessToken
    }
    finally {
        $accessToken = $null
        $tokenResult = $null
    }
}

function Get-SecretVersionId {
    param(
        [Parameter(Mandatory = $true)][object]$Version,
        [Parameter(Mandatory = $true)][string]$SecretName
    )
    if ($Version -isnot [System.Management.Automation.PSCustomObject]) {
        throw "secret $SecretName has malformed enabled version metadata"
    }
    $nameProperty = $Version.PSObject.Properties['name']
    if ($null -eq $nameProperty -or $nameProperty.Value -isnot [string]) {
        throw "secret $SecretName has malformed enabled version metadata"
    }
    $resourceName = [string]$nameProperty.Value
    $prefix = @(
        "projects/$projectNumber/secrets/$SecretName/versions/",
        "projects/$ProjectId/secrets/$SecretName/versions/"
    ) | Where-Object {
        $resourceName.StartsWith($_, [System.StringComparison]::Ordinal)
    } | Select-Object -First 1
    if ($null -eq $prefix) {
        throw "secret $SecretName has malformed enabled version metadata"
    }
    $versionId = $resourceName.Substring($prefix.Length)
    [uint64]$versionNumber = 0
    if (
        $versionId -cnotmatch '^[1-9][0-9]*$' -or
        -not [uint64]::TryParse($versionId, [ref]$versionNumber) -or
        $versionNumber -eq 0
    ) {
        throw "secret $SecretName has malformed enabled version metadata"
    }
    return $versionId
}

function Get-EnabledSecretVersions {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$AccessToken
    )
    $projectSegment = [System.Uri]::EscapeDataString($ProjectId)
    $secretSegment = [System.Uri]::EscapeDataString($Name)
    $filter = [System.Uri]::EscapeDataString('state:ENABLED')
    $uri = [System.Uri]::new(
        "$script:SecretManagerApiBaseUri/v1/projects/$projectSegment/secrets/$secretSegment/versions?filter=$filter"
    )
    $response = Invoke-SecretManagerRequest -Method 'GET' -Uri $uri `
        -AccessToken $AccessToken -TimeoutSeconds $ProcessTimeoutSeconds
    if ($response -isnot [System.Management.Automation.PSCustomObject]) {
        throw "secret $Name has malformed enabled version metadata"
    }
    $nextPageProperty = $response.PSObject.Properties['nextPageToken']
    if (
        $null -ne $nextPageProperty -and
        -not [string]::IsNullOrEmpty([string]$nextPageProperty.Value)
    ) {
        throw "secret $Name has ambiguous paginated enabled versions"
    }
    $versionsProperty = $response.PSObject.Properties['versions']
    if ($null -eq $versionsProperty) {
        return
    }
    if ($versionsProperty.Value -isnot [System.Array]) {
        throw "secret $Name has malformed enabled version metadata"
    }
    $versions = @()
    foreach ($version in @($versionsProperty.Value)) {
        $versionId = Get-SecretVersionId -Version $version -SecretName $Name
        $stateProperty = $version.PSObject.Properties['state']
        if (
            $null -eq $stateProperty -or
            $stateProperty.Value -isnot [string] -or
            [string]$stateProperty.Value -cne 'ENABLED'
        ) {
            throw "secret $Name has malformed enabled version metadata"
        }
        $versions += [pscustomobject]@{
            Id = $versionId
            SortKey = [uint64]$versionId
        }
    }
    return @($versions | Sort-Object SortKey)
}

function Get-SecretPayloadBytes {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$VersionId,
        [Parameter(Mandatory = $true)][string]$AccessToken
    )
    $projectSegment = [System.Uri]::EscapeDataString($ProjectId)
    $secretSegment = [System.Uri]::EscapeDataString($Name)
    $versionSegment = [System.Uri]::EscapeDataString($VersionId)
    $uri = [System.Uri]::new(
        "$script:SecretManagerApiBaseUri/v1/projects/$projectSegment/secrets/$secretSegment/versions/${versionSegment}:access"
    )
    $response = Invoke-SecretManagerRequest -Method 'GET' -Uri $uri `
        -AccessToken $AccessToken -TimeoutSeconds $ProcessTimeoutSeconds
    $payloadProperty = $response.PSObject.Properties['payload']
    if (
        $null -eq $payloadProperty -or
        $payloadProperty.Value -isnot [System.Management.Automation.PSCustomObject]
    ) {
        throw "secret $Name version $VersionId returned a malformed payload"
    }
    $dataProperty = $payloadProperty.Value.PSObject.Properties['data']
    if ($null -eq $dataProperty -or $dataProperty.Value -isnot [string]) {
        throw "secret $Name version $VersionId returned a malformed payload"
    }
    $encodedPayload = [string]$dataProperty.Value
    try {
        [byte[]]$payloadBytes = [Convert]::FromBase64String($encodedPayload)
    }
    catch {
        throw "secret $Name version $VersionId returned a malformed payload"
    }
    if ([Convert]::ToBase64String($payloadBytes) -cne $encodedPayload) {
        throw "secret $Name version $VersionId returned a malformed payload"
    }
    return ,$payloadBytes
}

function Get-SecretPayloadClassification {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$VersionId
    )
    if ($Bytes.Length -eq 43) {
        $value = [System.Text.Encoding]::ASCII.GetString($Bytes)
        if ($value -cmatch '^[A-Za-z0-9_-]{43}$') {
            return [pscustomobject]@{ Kind = 'canonical'; Value = $value }
        }
    }
    elseif ($Bytes.Length -eq 45 -and $Bytes[43] -eq 13 -and $Bytes[44] -eq 10) {
        $value = [System.Text.Encoding]::ASCII.GetString($Bytes, 0, 43)
        if ($value -cmatch '^[A-Za-z0-9_-]{43}$') {
            return [pscustomobject]@{ Kind = 'legacy-crlf'; Value = $value }
        }
    }
    throw "secret $Name version $VersionId has a non-canonical enabled payload"
}

function Add-CanonicalSecretVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$AccessToken
    )
    if ($Value -cnotmatch '^[A-Za-z0-9_-]{43}$') {
        throw "secret $Name canonical value is invalid"
    }
    [byte[]]$secretBytes = [System.Text.Encoding]::ASCII.GetBytes($Value)
    $encodedPayload = [Convert]::ToBase64String($secretBytes)
    $body = [ordered]@{
        payload = [ordered]@{ data = $encodedPayload }
    }
    $projectSegment = [System.Uri]::EscapeDataString($ProjectId)
    $secretSegment = [System.Uri]::EscapeDataString($Name)
    $uri = [System.Uri]::new(
        "$script:SecretManagerApiBaseUri/v1/projects/$projectSegment/secrets/${secretSegment}:addVersion"
    )
    try {
        $response = Invoke-SecretManagerRequest -Method 'POST' -Uri $uri `
            -AccessToken $AccessToken -Body $body -TimeoutSeconds $ProcessTimeoutSeconds
        $versionId = Get-SecretVersionId -Version $response -SecretName $Name
        $stateProperty = $response.PSObject.Properties['state']
        if (
            $null -eq $stateProperty -or
            $stateProperty.Value -isnot [string] -or
            [string]$stateProperty.Value -cne 'ENABLED'
        ) {
            throw "secret $Name addVersion returned malformed version metadata"
        }
        [byte[]]$readBackBytes = Get-SecretPayloadBytes -Name $Name `
            -VersionId $versionId -AccessToken $AccessToken
        if (-not [System.Security.Cryptography.CryptographicOperations]::FixedTimeEquals(
            $secretBytes,
            $readBackBytes
        )) {
            throw "secret $Name addVersion read-back did not match the canonical payload"
        }
        return $versionId
    }
    finally {
        $body = $null
        $encodedPayload = $null
        $response = $null
        $readBackBytes = $null
        $secretBytes = $null
    }
}

function Disable-SecretVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$VersionId,
        [Parameter(Mandatory = $true)][string]$AccessToken
    )
    $projectSegment = [System.Uri]::EscapeDataString($ProjectId)
    $secretSegment = [System.Uri]::EscapeDataString($Name)
    $versionSegment = [System.Uri]::EscapeDataString($VersionId)
    $uri = [System.Uri]::new(
        "$script:SecretManagerApiBaseUri/v1/projects/$projectSegment/secrets/$secretSegment/versions/${versionSegment}:disable"
    )
    $response = Invoke-SecretManagerRequest -Method 'POST' -Uri $uri `
        -AccessToken $AccessToken -Body ([ordered]@{}) -TimeoutSeconds $ProcessTimeoutSeconds
    $responseVersionId = Get-SecretVersionId -Version $response -SecretName $Name
    $stateProperty = $response.PSObject.Properties['state']
    if (
        $responseVersionId -cne $VersionId -or
        $null -eq $stateProperty -or
        $stateProperty.Value -isnot [string] -or
        [string]$stateProperty.Value -cne 'DISABLED'
    ) {
        throw "secret $Name disable returned malformed version metadata"
    }
}

function Ensure-Secret {
    param([string]$Name, [object[]]$ExistingSecrets)
    $exists = $ExistingSecrets | Where-Object {
        ($_.name -split '/')[-1] -eq $Name
    }
    if (-not $exists) {
        $null = Invoke-Gcloud -Arguments @(
            'secrets', 'create', $Name,
            '--replication-policy=automatic',
            "--project=$ProjectId",
            '--quiet'
        )
    }
    $secretDescription = (Invoke-GcloudJson -Arguments @(
        'secrets', 'describe', $Name, "--project=$ProjectId"
    ))[0]
    $replication = $secretDescription.replication
    if (
        $null -eq $replication -or
        $null -eq $replication.PSObject.Properties['automatic'] -or
        $null -ne $replication.PSObject.Properties['userManaged']
    ) {
        throw "secret $Name does not use exact automatic replication"
    }
    $accessToken = Get-GcloudAccessToken
    try {
        $versions = @(Get-EnabledSecretVersions -Name $Name -AccessToken $accessToken)
        if ($versions.Count -gt 2) {
            throw "secret $Name has ambiguous enabled versions"
        }
        $canonicalVersions = @()
        $legacyVersions = @()
        foreach ($version in $versions) {
            [byte[]]$payloadBytes = Get-SecretPayloadBytes -Name $Name `
                -VersionId $version.Id -AccessToken $accessToken
            $classification = Get-SecretPayloadClassification -Bytes $payloadBytes `
                -Name $Name -VersionId $version.Id
            $record = [pscustomobject]@{
                Id = $version.Id
                Value = $classification.Value
            }
            if ($classification.Kind -ceq 'canonical') {
                $canonicalVersions += $record
            }
            else {
                $legacyVersions += $record
            }
            $payloadBytes = $null
            $classification = $null
            $record = $null
        }

        if ($versions.Count -eq 0) {
            $canonicalValue = New-RandomSecret
            $targetVersionId = Add-CanonicalSecretVersion -Name $Name `
                -Value $canonicalValue -AccessToken $accessToken
        }
        elseif ($canonicalVersions.Count -eq 1 -and $legacyVersions.Count -eq 0) {
            $canonicalValue = $canonicalVersions[0].Value
            $targetVersionId = $canonicalVersions[0].Id
        }
        elseif ($canonicalVersions.Count -eq 0 -and $legacyVersions.Count -eq 1) {
            $canonicalValue = $legacyVersions[0].Value
            $targetVersionId = Add-CanonicalSecretVersion -Name $Name `
                -Value $canonicalValue -AccessToken $accessToken
            Disable-SecretVersion -Name $Name -VersionId $legacyVersions[0].Id `
                -AccessToken $accessToken
        }
        elseif (
            $canonicalVersions.Count -eq 1 -and
            $legacyVersions.Count -eq 1 -and
            $canonicalVersions[0].Value -ceq $legacyVersions[0].Value
        ) {
            $canonicalValue = $canonicalVersions[0].Value
            $targetVersionId = $canonicalVersions[0].Id
            Disable-SecretVersion -Name $Name -VersionId $legacyVersions[0].Id `
                -AccessToken $accessToken
        }
        else {
            throw "secret $Name has ambiguous enabled versions"
        }

        $finalVersions = @(Get-EnabledSecretVersions -Name $Name -AccessToken $accessToken)
        if ($finalVersions.Count -ne 1 -or $finalVersions[0].Id -cne $targetVersionId) {
            throw "secret $Name did not converge to exactly one canonical enabled version"
        }
        [byte[]]$finalBytes = Get-SecretPayloadBytes -Name $Name `
            -VersionId $targetVersionId -AccessToken $accessToken
        $finalClassification = Get-SecretPayloadClassification -Bytes $finalBytes `
            -Name $Name -VersionId $targetVersionId
        if (
            $finalClassification.Kind -cne 'canonical' -or
            $finalClassification.Value -cne $canonicalValue
        ) {
            throw "secret $Name did not converge to exactly one canonical enabled version"
        }
        return $canonicalValue
    }
    finally {
        $accessToken = $null
        $canonicalValue = $null
        $finalBytes = $null
        $finalClassification = $null
    }
}

function Get-IamPolicyBindings {
    param(
        [Parameter(Mandatory = $true)][AllowNull()][object]$Policy,
        [Parameter(Mandatory = $true)][string]$PolicyName
    )
    if ($null -eq $Policy -or $Policy -isnot [System.Management.Automation.PSCustomObject]) {
        throw "$PolicyName IAM policy has malformed bindings"
    }
    $bindingsProperty = $Policy.PSObject.Properties['bindings']
    if ($null -eq $bindingsProperty -or $null -eq $bindingsProperty.Value) {
        return
    }
    if ($bindingsProperty.Value -isnot [System.Array]) {
        throw "$PolicyName IAM policy has malformed bindings"
    }
    $bindings = @($bindingsProperty.Value)
    foreach ($binding in $bindings) {
        if ($null -eq $binding) {
            throw "$PolicyName IAM policy has malformed bindings"
        }
        $roleProperty = $binding.PSObject.Properties['role']
        $membersProperty = $binding.PSObject.Properties['members']
        if (
            $null -eq $roleProperty -or
            $roleProperty.Value -isnot [string] -or
            [string]::IsNullOrWhiteSpace([string]$roleProperty.Value) -or
            $null -eq $membersProperty -or
            $membersProperty.Value -isnot [System.Array]
        ) {
            throw "$PolicyName IAM policy has malformed bindings"
        }
        $members = @($membersProperty.Value)
        if (
            $members.Count -eq 0 -or
            @($members | Where-Object {
                $_ -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$_)
            }).Count -gt 0
        ) {
            throw "$PolicyName IAM policy has malformed bindings"
        }
    }
    return $bindings
}

function Ensure-ProjectRoles {
    $requiredRoles = @('roles/aiplatform.user', 'roles/cloudsql.client')
    $member = "serviceAccount:$serviceAccount"
    $policy = Invoke-GcloudJson -Arguments @(
        'projects', 'get-iam-policy', $ProjectId,
        "--project=$ProjectId"
    )
    $bindings = @(Get-IamPolicyBindings -Policy $policy `
        -PolicyName 'runtime service account project')
    $memberBindings = @($bindings | Where-Object { $_.members -contains $member })
    foreach ($binding in $memberBindings) {
        if (
            $binding.role -notin $requiredRoles -or
            $null -ne $binding.PSObject.Properties['condition']
        ) {
            throw 'runtime service account project IAM has extra or conditional roles'
        }
    }
    foreach ($role in $requiredRoles) {
        if (-not ($memberBindings | Where-Object { $_.role -eq $role })) {
            $null = Invoke-Gcloud -Arguments @(
                'projects', 'add-iam-policy-binding', $ProjectId,
                '--member', $member,
                "--role=$role",
                '--condition=None',
                "--project=$ProjectId",
                '--quiet'
            )
        }
    }
}

function Ensure-SecretRole {
    param([string]$Name)
    $policy = Invoke-GcloudJson -Arguments @(
        'secrets', 'get-iam-policy', $Name,
        "--project=$ProjectId"
    )
    $member = "serviceAccount:$serviceAccount"
    $bindings = @(Get-IamPolicyBindings -Policy $policy -PolicyName "secret $Name")
    $memberBindings = @($bindings | Where-Object { $_.members -contains $member })
    if ($memberBindings | Where-Object {
        $_.role -ne 'roles/secretmanager.secretAccessor' -or
        $null -ne $_.PSObject.Properties['condition']
    }) {
        throw "secret $Name IAM has extra or conditional runtime bindings"
    }
    if (-not $memberBindings) {
        $null = Invoke-Gcloud -Arguments @(
            'secrets', 'add-iam-policy-binding', $Name,
            '--member', $member,
            '--role=roles/secretmanager.secretAccessor',
            '--condition=None',
            "--project=$ProjectId",
            '--quiet'
        )
    }
}

function Ensure-BucketRole {
    $policy = Invoke-GcloudJson -Arguments @(
        'storage', 'buckets', 'get-iam-policy', "gs://$bucket",
        "--project=$ProjectId"
    )
    $member = "serviceAccount:$serviceAccount"
    $bindings = @(Get-IamPolicyBindings -Policy $policy -PolicyName 'release bucket')
    $memberBindings = @($bindings | Where-Object { $_.members -contains $member })
    if ($memberBindings | Where-Object {
        $_.role -ne 'roles/storage.objectViewer' -or
        $null -ne $_.PSObject.Properties['condition']
    }) {
        throw 'release bucket IAM has extra or conditional runtime bindings'
    }
    if (-not $memberBindings) {
        $null = Invoke-Gcloud -Arguments @(
            'storage', 'buckets', 'add-iam-policy-binding', "gs://$bucket",
            '--member', $member,
            '--role=roles/storage.objectViewer',
            "--project=$ProjectId",
            '--quiet'
        )
    }
}

# Enable only the APIs declared by the approved demo design.
$enabled = @(Invoke-GcloudJson -Arguments @(
    'services', 'list', '--enabled', "--project=$ProjectId"
))
$enabledNames = @($enabled | ForEach-Object { $_.config.name })
$missingApis = @($apis | Where-Object { $_ -notin $enabledNames })
if ($missingApis.Count -gt 0) {
    $enableArguments = @('services', 'enable') + $missingApis + @(
        "--project=$ProjectId", '--quiet'
    )
    $null = Invoke-Gcloud -Arguments $enableArguments
}

$serviceAccounts = @(Invoke-GcloudJson -Arguments @(
    'iam', 'service-accounts', 'list', "--project=$ProjectId"
))
if (-not ($serviceAccounts | Where-Object { $_.email -eq $serviceAccount })) {
    $null = Invoke-Gcloud -Arguments @(
        'iam', 'service-accounts', 'create', $serviceAccountName,
        '--display-name=HK Movie RAG demo runtime',
        "--project=$ProjectId",
        '--quiet'
    )
}
elseif (($serviceAccounts | Where-Object { $_.email -eq $serviceAccount }).disabled -ne $false) {
    throw 'runtime service account exists but is disabled'
}

$repositories = @(Invoke-GcloudJson -Arguments @(
    'artifacts', 'repositories', 'list', "--location=$Region", "--project=$ProjectId"
))
$existingRepository = $repositories | Where-Object { $_.name -match "/repositories/$repository$" }
if (-not $existingRepository) {
    $null = Invoke-Gcloud -Arguments @(
        'artifacts', 'repositories', 'create', $repository,
        '--repository-format=docker',
        "--location=$Region",
        '--description=HK Movie minimum RAG demo images',
        "--project=$ProjectId",
        '--quiet'
    )
}
elseif ($existingRepository.format -ne 'DOCKER' -or $existingRepository.name -notmatch "/locations/$Region/") {
    throw 'Artifact Registry repository exists with incompatible configuration'
}

$buckets = @(Invoke-GcloudJson -Arguments @(
    'storage', 'buckets', 'list', "--project=$ProjectId"
))
$existingBucket = $buckets | Where-Object { $_.name -eq $bucket }
if (-not $existingBucket) {
    $null = Invoke-Gcloud -Arguments @(
        'storage', 'buckets', 'create', "gs://$bucket",
        "--location=$Region",
        '--uniform-bucket-level-access',
        '--public-access-prevention',
        "--project=$ProjectId",
        '--quiet'
    )
    $existingBucket = (Invoke-GcloudJson -Arguments @(
        'storage', 'buckets', 'describe', "gs://$bucket", "--project=$ProjectId"
    ))[0]
}
$uniformEnabled = $existingBucket.uniform_bucket_level_access
$publicPrevention = $existingBucket.public_access_prevention
if (
    $existingBucket.location -ne 'US-CENTRAL1' -or
    $uniformEnabled -ne $true -or
    $publicPrevention -ne 'enforced'
) {
    throw 'release bucket exists with incompatible location or access controls'
}

$instances = @(Invoke-GcloudJson -Arguments @(
    'sql', 'instances', 'list', "--project=$ProjectId"
))
$existingInstance = $instances | Where-Object { $_.name -eq $instance }
if (-not $existingInstance) {
    $null = Invoke-Gcloud -Arguments @(
        'sql', 'instances', 'create', $instance,
        '--database-version=POSTGRES_16',
        '--edition=ENTERPRISE',
        '--tier=db-g1-small',
        '--storage-size=10',
        '--storage-type=SSD',
        '--availability-type=zonal',
        '--no-storage-auto-increase',
        '--deletion-protection',
        "--region=$Region",
        "--project=$ProjectId",
        '--quiet'
    )
    $existingInstance = (Invoke-GcloudJson -Arguments @(
        'sql', 'instances', 'describe', $instance, "--project=$ProjectId"
    ))[0]
}
$instanceSettings = $existingInstance.settings
$editionProperty = $instanceSettings.PSObject.Properties['edition']
$instanceEdition = if ($null -eq $editionProperty) { $null } else { $editionProperty.Value }
if (
    $existingInstance.databaseVersion -ne 'POSTGRES_16' -or
    $existingInstance.region -ne $Region -or
    $instanceEdition -cne 'ENTERPRISE' -or
    $instanceSettings.tier -ne 'db-g1-small' -or
    [int]$instanceSettings.dataDiskSizeGb -ne 10 -or
    $instanceSettings.dataDiskType -ne 'PD_SSD' -or
    $instanceSettings.availabilityType -ne 'ZONAL' -or
    $instanceSettings.storageAutoResize -ne $false -or
    $instanceSettings.deletionProtectionEnabled -ne $true
) {
    throw 'Cloud SQL instance exists with incompatible immutable demo configuration'
}

$existingSecrets = @(Invoke-GcloudJson -Arguments @(
    'secrets', 'list', "--project=$ProjectId"
))
$databasePassword = Ensure-Secret -Name $dbSecret -ExistingSecrets $existingSecrets
$null = Ensure-Secret -Name $demoSecret -ExistingSecrets $existingSecrets

$databases = @(Invoke-GcloudJson -Arguments @(
    'sql', 'databases', 'list', "--instance=$instance", "--project=$ProjectId"
))
if (-not ($databases | Where-Object { $_.name -eq $database })) {
    $null = Invoke-Gcloud -Arguments @(
        'sql', 'databases', 'create', $database,
        "--instance=$instance",
        "--project=$ProjectId",
        '--quiet'
    )
}

$users = @(Invoke-GcloudJson -Arguments @(
    'sql', 'users', 'list', "--instance=$instance", "--project=$ProjectId"
))
$targetUsers = @($users | Where-Object {
    $null -ne $_ -and
    $null -ne $_.PSObject.Properties['name'] -and
    [string]$_.PSObject.Properties['name'].Value -ceq $databaseUser
})
$databaseUserExists = $false
if ($targetUsers.Count -gt 1) {
    throw 'Cloud SQL user exists with incompatible or ambiguous identity'
}
if ($targetUsers.Count -eq 1) {
    $targetUser = $targetUsers[0]
    $typeProperty = $targetUser.PSObject.Properties['type']
    if ($null -ne $typeProperty) {
        if ([string]$typeProperty.Value -cne 'BUILT_IN') {
            throw 'Cloud SQL user exists with incompatible or ambiguous identity'
        }
    }
    else {
        $etagProperty = $targetUser.PSObject.Properties['etag']
        $hostProperty = $targetUser.PSObject.Properties['host']
        $instanceProperty = $targetUser.PSObject.Properties['instance']
        $kindProperty = $targetUser.PSObject.Properties['kind']
        $projectProperty = $targetUser.PSObject.Properties['project']
        if (
            $null -eq $etagProperty -or
            [string]::IsNullOrWhiteSpace([string]$etagProperty.Value) -or
            $null -eq $hostProperty -or
            [string]$hostProperty.Value -cne '' -or
            $null -eq $instanceProperty -or
            [string]$instanceProperty.Value -cne $instance -or
            $null -eq $kindProperty -or
            [string]$kindProperty.Value -cne 'sql#user' -or
            $null -eq $projectProperty -or
            [string]$projectProperty.Value -cne $ProjectId
        ) {
            throw 'Cloud SQL user exists with incompatible or ambiguous identity'
        }
    }
    $databaseUserExists = $true
}
try {
    Set-CloudSqlBuiltInUserPassword -Password $databasePassword `
        -UserExists $databaseUserExists
}
finally {
    $databasePassword = $null
}

Ensure-ProjectRoles
Ensure-BucketRole
Ensure-SecretRole -Name $dbSecret
Ensure-SecretRole -Name $demoSecret

$result = New-ProvisionPlan -Mode 'apply'
$result.Remove('commands')
$result['status'] = 'applied'
$result | ConvertTo-Json -Depth 8 -Compress
