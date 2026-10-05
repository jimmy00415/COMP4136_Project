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
    [string]$SourceRoot = 'D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG',

    [Parameter()]
    [string]$RagConfigPath = 'config/rag_demo_r2.yaml',

    [Parameter()]
    [string]$DocumentSourceRoot = '',

    [Parameter()]
    [string]$ReuseFromReleaseId = 'v1.2-demo',

    [Parameter()]
    [ValidateSet('v1.2-demo-r2', 'v1.2-demo-r3')]
    [string]$ExpectedRagReleaseId = 'v1.2-demo-r2',

    [Parameter()]
    [string]$BundleDirectory = '',

    [Parameter(DontShow = $true)]
    [ValidateRange(0, 60000)]
    [int]$TestJobPollMilliseconds = 0,

    [Parameter(DontShow = $true)]
    [ValidateRange(0, 86400)]
    [int]$TestJobDeadlineSeconds = 0,

    [Parameter(DontShow = $true)]
    [ValidateRange(0, 3600)]
    [int]$TestServiceDeployTimeoutSeconds = 0,

    [Parameter(DontShow = $true)]
    [string]$TestCloudRunV2Origin = '',

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
$serviceAccount = "hk-rag-runtime@$ProjectId.iam.gserviceaccount.com"
$dbSecret = 'hk-rag-db-password'
$job = 'hk-movie-rag-ingest'
$posterVerifyJob = 'hk-movie-rag-poster-verify'
$service = 'hk-movie-rag-demo'
$posterPrefix = 'assets/posters/derived/'

$deploymentContract = if ($ExpectedRagReleaseId -ceq 'v1.2-demo-r3') {
    [ordered]@{
        release_id = 'v1.2-demo-r3'
        reuse_from_release_id = 'v1.2-demo-r2'
        rag_schema_version = '1.3'
        parent_release_manifest_sha256 = 'e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c'
        movie_count = 4659
        facet_count = 4659
        tier_s_count = 51
        tier_a_count = 313
        tier_b_count = 4295
        pilot_count = 24
        poster_count = 4546
        unavailable_poster_count = 113
        document_count = 5
        pdf_passage_count = 21
        reusable_count = 4670
        reuse_release_embedding_count = 4670
        new_embedding_count = 10
        embedding_count = 4680
        relevance_policy_sha256 = 'd6db5e118aeea5dc0484f3c591baac666952f78f4d41bfdcdbbb58693f4f6bba'
        poster_authority_sha256 = 'c50ff06a88f91296b63bd74a1e4a6485785bdc4c07e73d09b407a126927c8137'
        reuse_manifest_prefix = 'rag/v1.2-demo-r2'
        reuse_manifest_sha256 = 'b12238477ad8fd4b78ffa94a579bdaa13bb778f32249c431dcc76437abad1efd'
        reuse_manifest_generation = '1786355621564439'
        reuse_manifest_size = 2706
        reuse_bundle_sha256 = '3398d553d2d1032ee892584aefcb4f03720dfafd8e7b4d2f7bfc28f0fdd3b882'
        reuse_bundle_generation = '1786355644271302'
        reuse_bundle_size = 10939267
        candidate_suffix = 'r3'
        default_bundle_directory = 'artifacts/rag-demo/v1.2-demo-r3-deploy'
    }
}
else {
    [ordered]@{
        release_id = 'v1.2-demo-r2'
        reuse_from_release_id = 'v1.2-demo'
        rag_schema_version = '1.2'
        parent_release_manifest_sha256 = 'e9842db33208e7146d78d86689b13f81fffcf41288d79d84e599b93050445e2c'
        movie_count = 4658
        facet_count = 4658
        tier_s_count = 50
        tier_a_count = 313
        tier_b_count = 4295
        pilot_count = 24
        poster_count = 4545
        unavailable_poster_count = 113
        document_count = 3
        pdf_passage_count = 12
        reusable_count = 4658
        reuse_release_embedding_count = 4664
        new_embedding_count = 12
        embedding_count = 4670
        relevance_policy_sha256 = 'a4ecce727a469689a2a08c6d42e0d7d1eb975761173e3b6e4d1eaf34db7b2aba'
        poster_authority_sha256 = '9e56adda2a1fdfa648869c53d7e60a085db5d1c6d11db90dbec13bfe12f0eeed'
        reuse_manifest_prefix = 'rag/v1.2-demo-mvp1'
        reuse_manifest_sha256 = '18be34d0c36d0d010fceb1b3ff4c2dea734479f899f8ef28ae7541d8f43fc9a0'
        reuse_manifest_generation = '1785741058239832'
        reuse_manifest_size = 2523
        reuse_bundle_sha256 = 'beb745fe0fa2944cff594c445b5bb7b7097c6d103aba02f4c8a762142d4cc30a'
        reuse_bundle_generation = '1785741077175031'
        reuse_bundle_size = 10926913
        candidate_suffix = 'r2'
        default_bundle_directory = 'artifacts/rag-demo/v1.2-demo-r2-deploy'
    }
}
if ($ReuseFromReleaseId -cne $deploymentContract.reuse_from_release_id) {
    throw 'reuse source release ID does not match the selected immutable deployment contract'
}

function New-DeployPlan {
    param([string]$Mode)
    [ordered]@{
        schema_version = 'rag-demo-deploy/v1'
        mode = $Mode
        project_id = $ProjectId
        region = $Region
        release_bucket = $bucket
        inputs = [ordered]@{
            rag_config_path = $RagConfigPath
            document_source_root = $DocumentSourceRoot
            expected_rag_release_id = $ExpectedRagReleaseId
            reuse_from_release_id = $ReuseFromReleaseId
        }
        release_authority = 'verify-rag-bundle JSON'
        poster_upload = [ordered]@{
            count = $deploymentContract.poster_count
            content_type = 'image/webp'
            destination_prefix = $posterPrefix
            public = $false
        }
        bundle_upload = [ordered]@{
            destination_prefix = '[FROM_VERIFIED_RELEASE]'
            immutable_create_only = $true
        }
        image_template = "$Region-docker.pkg.dev/$ProjectId/$repository/demo:<git-sha-12>"
        cloud_run_job = $job
        poster_verify_job = $posterVerifyJob
        cloud_run_service = $service
        service_iam = 'invoker_iam_check_disabled_with_app_access_gate'
        gcs_publication = $false
        steps = @(
            'verify_source_release',
            'build_and_verify_bundle',
            'validate_exact_poster_allowlist',
            'upload_private_release',
            'build_image',
            'deploy_ingest_job',
            'execute_and_wait_ingest_job',
            "require_$($deploymentContract.reusable_count)_reusable_and_$($deploymentContract.new_embedding_count)_new_final_state",
            'execute_and_wait_active_noop_rerun',
            'deploy_poster_verify_job',
            'execute_and_wait_poster_verify_job',
            'deploy_zero_traffic_tagged_candidate',
            'authenticated_candidate_smoke',
            'conditional_promotion',
            'authenticated_stable_smoke',
            'conditional_tag_cleanup',
            'emit_redacted_result'
        )
    }
}

if ($PlanOnly) {
    if (-not [string]::IsNullOrEmpty($TestCloudRunV2Origin)) {
        throw 'Cloud Run v2 endpoint overrides are unavailable in plan mode'
    }
    New-DeployPlan -Mode 'plan' | ConvertTo-Json -Depth 8 -Compress
    exit 0
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$liveSmokeReportDirectory = Join-Path $repoRoot 'artifacts/rag-demo'
$script:LiveSmokeReportPath = ''
$script:LiveSmokeReportRelativePath = ''
$script:LiveSmokeExpectedBaseUrl = ''
$resolvedSourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
$ragConfigCandidate = if ([System.IO.Path]::IsPathRooted($RagConfigPath)) {
    $RagConfigPath
}
else {
    Join-Path $repoRoot $RagConfigPath
}
$resolvedRagConfigPath = (Resolve-Path -LiteralPath $ragConfigCandidate).Path
if (-not (Test-Path -LiteralPath $resolvedRagConfigPath -PathType Leaf)) {
    throw 'RAG config path must resolve to one file'
}
$documentSourceCandidate = if ([string]::IsNullOrWhiteSpace($DocumentSourceRoot)) {
    $resolvedSourceRoot
}
else {
    $DocumentSourceRoot
}
$resolvedDocumentSourceRoot = (Resolve-Path -LiteralPath $documentSourceCandidate).Path
if (-not (Test-Path -LiteralPath $resolvedDocumentSourceRoot -PathType Container)) {
    throw 'document source root must resolve to one directory'
}
if (
    $ReuseFromReleaseId -cnotmatch '^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$' -or
    $ReuseFromReleaseId.Contains('..')
) {
    throw 'reuse source release ID is unsafe'
}
if ([string]::IsNullOrWhiteSpace($BundleDirectory)) {
    $BundleDirectory = Join-Path $repoRoot $deploymentContract.default_bundle_directory
}
$bundlePathCandidate = [System.IO.Path]::GetFullPath($BundleDirectory)
$allowedBundleRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $repoRoot 'artifacts/rag-demo')
).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not ($bundlePathCandidate + [System.IO.Path]::DirectorySeparatorChar).StartsWith(
    $allowedBundleRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'bundle directory must remain below the code worktree artifacts/rag-demo directory'
}
$BundleDirectory = $bundlePathCandidate

$gcloudCommand = Get-Command gcloud -ErrorAction Stop
$script:Gcloud = $gcloudCommand.Source
$jobPollMilliseconds = 15000
$jobDeadlineSeconds = 86400
$cloudRunV2PollMilliseconds = 1000
$cloudRunV2DeadlineSeconds = 600
$serviceDeployTimeoutSeconds = 3600
if ($TestJobPollMilliseconds -ne 0 -or $TestJobDeadlineSeconds -ne 0) {
    if (
        $TestJobPollMilliseconds -eq 0 -or
        $TestJobDeadlineSeconds -eq 0 -or
        $env:RAG_DEMO_TEST_MODE -ne 'offline-fake-gcloud' -or
        [string]::IsNullOrWhiteSpace($env:PYTEST_CURRENT_TEST) -or
        [System.IO.Path]::GetExtension($script:Gcloud) -ne '.ps1'
    ) {
        throw 'Job polling overrides are restricted to the offline pytest fake-gcloud gate'
    }
    $jobPollMilliseconds = $TestJobPollMilliseconds
    $jobDeadlineSeconds = $TestJobDeadlineSeconds
    $cloudRunV2PollMilliseconds = $TestJobPollMilliseconds
    $cloudRunV2DeadlineSeconds = $TestJobDeadlineSeconds
}
if ($TestServiceDeployTimeoutSeconds -ne 0) {
    if (
        $env:RAG_DEMO_TEST_MODE -ne 'offline-fake-gcloud' -or
        [string]::IsNullOrWhiteSpace($env:PYTEST_CURRENT_TEST) -or
        [System.IO.Path]::GetExtension($script:Gcloud) -ne '.ps1' -or
        $TestJobPollMilliseconds -eq 0 -or
        $TestJobDeadlineSeconds -eq 0
    ) {
        throw 'Service deploy timeout override is restricted to the offline pytest gate'
    }
    $serviceDeployTimeoutSeconds = $TestServiceDeployTimeoutSeconds
}

$script:CloudRunV2Origin = 'https://run.googleapis.com'
$script:CloudRunV2UsesTestCredential = $false
if (-not [string]::IsNullOrEmpty($TestCloudRunV2Origin)) {
    if (
        $env:RAG_DEMO_TEST_MODE -cne 'offline-fake-gcloud' -or
        [string]::IsNullOrWhiteSpace($env:PYTEST_CURRENT_TEST) -or
        [System.IO.Path]::GetExtension($script:Gcloud) -cne '.ps1' -or
        $TestJobPollMilliseconds -eq 0 -or
        $TestJobDeadlineSeconds -eq 0
    ) {
        throw 'Cloud Run v2 endpoint overrides are restricted to the exact offline pytest fixture'
    }
    $testOrigin = $null
    if (
        -not [System.Uri]::TryCreate(
            $TestCloudRunV2Origin,
            [System.UriKind]::Absolute,
            [ref]$testOrigin
        ) -or
        $testOrigin.Scheme -cne [System.Uri]::UriSchemeHttp -or
        $testOrigin.Host -cne '127.0.0.1' -or
        $testOrigin.IsDefaultPort -or
        $testOrigin.Port -lt 1 -or
        -not [string]::IsNullOrEmpty($testOrigin.UserInfo) -or
        -not [string]::IsNullOrEmpty($testOrigin.Query) -or
        -not [string]::IsNullOrEmpty($testOrigin.Fragment) -or
        $testOrigin.AbsolutePath -cne '/'
    ) {
        throw 'Cloud Run v2 test endpoint is not one exact loopback HTTP origin'
    }
    $script:CloudRunV2Origin = $testOrigin.GetLeftPart([System.UriPartial]::Authority)
    $script:CloudRunV2UsesTestCredential = $true
}

function New-GcloudStartInfo {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    foreach ($ambientGoogleVariable in @(
        'GOOGLE_API_KEY',
        'GOOGLE_CLOUD_LOCATION',
        'GOOGLE_CLOUD_PROJECT',
        'RAG_DEMO_ACCESS_KEY'
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
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    return $startInfo
}

function Invoke-GcloudResult {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure,
        [switch]$CaptureTimeoutFailure,
        [ValidateRange(1, 86400)][int]$TimeoutSeconds = 3600
    )
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = New-GcloudStartInfo -Arguments $Arguments
    try {
        if (-not $process.Start()) {
            throw 'gcloud process did not start'
        }
        $standardOutput = $process.StandardOutput.ReadToEndAsync()
        $standardError = $process.StandardError.ReadToEndAsync()
        $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
        if ($timedOut) {
            $process.Kill($true)
            $null = $process.WaitForExit(5000)
        }
        $output = $standardOutput.GetAwaiter().GetResult()
        $errorOutput = $standardError.GetAwaiter().GetResult()
        $exitCode = if ($timedOut) { -1 } else { $process.ExitCode }
    }
    finally {
        $standardOutput = $null
        $standardError = $null
        $process.Dispose()
    }
    if ($timedOut -and -not $CaptureTimeoutFailure) {
        throw "gcloud command exceeded its redacted $TimeoutSeconds-second deadline"
    }
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "gcloud command failed without exposing command output (exit $exitCode)"
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
        ErrorOutput = $errorOutput
        TimedOut = $timedOut
    }
}

function Invoke-Gcloud {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [ValidateRange(1, 86400)][int]$TimeoutSeconds = 3600
    )
    $result = Invoke-GcloudResult -Arguments $Arguments -TimeoutSeconds $TimeoutSeconds
    return $result.Output
}

function Invoke-GcloudJson {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [ValidateRange(1, 86400)][int]$TimeoutSeconds = 3600
    )
    $text = Invoke-Gcloud -Arguments ($Arguments + '--format=json') -TimeoutSeconds $TimeoutSeconds
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw 'gcloud returned no JSON result'
    }
    return ($text | ConvertFrom-Json)
}

function Invoke-GcloudObject {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [ValidateRange(1, 86400)][int]$TimeoutSeconds = 3600
    )
    $text = Invoke-Gcloud -Arguments ($Arguments + '--format=json') -TimeoutSeconds $TimeoutSeconds
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw 'gcloud returned no JSON object'
    }
    $document = [System.Text.Json.JsonDocument]::Parse($text)
    try {
        if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) {
            throw 'gcloud result is not one exact JSON object'
        }
    }
    finally {
        $document.Dispose()
    }
    $value = ConvertFrom-Json -InputObject $text -NoEnumerate
    if ($null -eq $value -or $value -isnot [pscustomobject]) {
        throw 'gcloud result is not one exact JSON object'
    }
    return $value
}

function Invoke-GcloudArray {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [ValidateRange(1, 86400)][int]$TimeoutSeconds = 3600
    )
    $text = Invoke-Gcloud -Arguments ($Arguments + '--format=json') -TimeoutSeconds $TimeoutSeconds
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw 'gcloud returned no JSON array'
    }
    $document = [System.Text.Json.JsonDocument]::Parse($text)
    try {
        if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Array) {
            throw 'gcloud result is not one exact JSON array'
        }
    }
    finally {
        $document.Dispose()
    }
    $value = ConvertFrom-Json -InputObject $text -NoEnumerate
    if ($null -eq $value -or $value -isnot [System.Array]) {
        throw 'gcloud result is not one exact JSON array'
    }
    foreach ($item in $value) {
        if ($null -eq $item -or $item -isnot [pscustomobject]) {
            throw 'gcloud JSON array contains a non-object entry'
        }
    }
    return $value
}

function Get-GcsObjectDescription {
    param([Parameter(Mandatory = $true)][string]$Uri)
    $result = Invoke-GcloudResult -Arguments @(
        'storage', 'objects', 'describe', $Uri,
        "--project=$ProjectId",
        '--format=json'
    ) -AllowFailure -TimeoutSeconds 300
    if ($result.ExitCode -eq 0) {
        if ([string]::IsNullOrWhiteSpace($result.Output)) {
            throw 'gcloud returned no object description JSON'
        }
        $description = ConvertFrom-Json -InputObject $result.Output -NoEnumerate
        if ($description -is [System.Array]) {
            throw 'gcloud object description must be one JSON object'
        }
        return $description
    }
    if ($result.ErrorOutput -match '(?i)(\b404\b|not[ -]?found)') {
        return $null
    }
    throw "gcloud object lookup failed without exposing command output (exit $($result.ExitCode))"
}

function Get-ImmutableReleaseObjectSha256 {
    param([Parameter(Mandatory = $true)][object]$Description)

    if ($null -eq $Description -or $Description -isnot [pscustomobject]) {
        throw 'immutable release object description is not a JSON object'
    }
    $customFieldsProperty = $Description.PSObject.Properties['custom_fields']
    $legacyMetadataProperty = $Description.PSObject.Properties['metadata']
    if ($null -ne $customFieldsProperty -and $null -ne $legacyMetadataProperty) {
        throw 'immutable release object metadata shape is ambiguous'
    }
    if ($null -eq $customFieldsProperty -and $null -eq $legacyMetadataProperty) {
        throw 'immutable release object metadata is missing'
    }

    # gcloud 553 emits custom_fields; retain the previous metadata shape for SDK compatibility.
    if ($null -ne $customFieldsProperty) {
        $fields = $customFieldsProperty.Value
    }
    else {
        $fields = $legacyMetadataProperty.Value
    }
    if ($null -eq $fields -or $fields -isnot [pscustomobject]) {
        throw 'immutable release object metadata is not a JSON object'
    }
    $fieldProperties = @($fields.PSObject.Properties)
    if (
        $fieldProperties.Count -ne 1 -or
        $fieldProperties[0].Name -cne 'sha256' -or
        $fieldProperties[0].Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace([string]$fieldProperties[0].Value)
    ) {
        throw 'immutable release object metadata is not the exact sha256 contract'
    }
    return [string]$fieldProperties[0].Value
}

function Assert-ImmutableReleaseObject {
    param(
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$ContentType,
        [Parameter(Mandatory = $true)][string]$Sha256
    )
    $description = Get-GcsObjectDescription -Uri $Uri
    if ($null -eq $description) {
        $createResult = Invoke-GcloudResult -Arguments @(
            'storage', 'cp', $LocalPath, $Uri,
            "--content-type=$ContentType",
            '--cache-control=no-store',
            "--custom-metadata=sha256=$Sha256",
            '--if-generation-match=0',
            "--project=$ProjectId",
            '--quiet'
        ) -AllowFailure
        if ($createResult.ExitCode -ne 0) {
            # A concurrent identical deployment may have won the generation-zero race.
            $description = Get-GcsObjectDescription -Uri $Uri
            if ($null -eq $description) {
                throw 'immutable release object creation failed without a verifiable winner'
            }
        }
        else {
            $description = Get-GcsObjectDescription -Uri $Uri
        }
    }
    $actualSha256 = Get-ImmutableReleaseObjectSha256 -Description $description
    if (
        $actualSha256 -cne $Sha256 -or
        [int64]$description.size -ne (Get-Item -LiteralPath $LocalPath).Length -or
        $description.content_type -ne $ContentType -or
        $description.cache_control -ne 'no-store'
    ) {
        throw 'immutable release object exists with different bytes or metadata'
    }
}

function New-LocalStartInfo {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    $command = Get-Command $Executable -ErrorAction Stop
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    if ([System.IO.Path]::GetExtension($command.Source) -eq '.ps1') {
        $startInfo.FileName = (Get-Process -Id $PID).Path
        foreach ($hostArgument in @(
            '-NoLogo', '-NoProfile', '-NonInteractive', '-File', $command.Source
        )) {
            $startInfo.ArgumentList.Add($hostArgument)
        }
    }
    else {
        $startInfo.FileName = $command.Source
    }
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }
    $startInfo.WorkingDirectory = $WorkingDirectory
    $null = $startInfo.Environment.Remove('RAG_DEMO_ACCESS_KEY')
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    return $startInfo
}

function Invoke-Local {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [ValidateRange(1, 86400)][int]$TimeoutSeconds = 1800
    )
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = New-LocalStartInfo `
        -Executable $Executable `
        -Arguments $Arguments `
        -WorkingDirectory $WorkingDirectory
    try {
        if (-not $process.Start()) {
            throw "$Executable process did not start"
        }
        $standardOutput = $process.StandardOutput.ReadToEndAsync()
        $standardError = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill($true)
            throw "$Executable exceeded its redacted $TimeoutSeconds-second deadline"
        }
        $output = $standardOutput.GetAwaiter().GetResult()
        $null = $standardError.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
    }
    finally {
        $standardOutput = $null
        $standardError = $null
        $process.Dispose()
    }
    if ($exitCode -ne 0) {
        throw "$Executable failed without exposing command output (exit $exitCode)"
    }
    return $output
}

function Invoke-LiveSmoke {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedRevision,
        [Parameter(Mandatory = $true)][string]$ExpectedImageDigest,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][ValidateSet('candidate', 'stable')][string]$Phase
    )
    if (-not (Test-Path -LiteralPath $liveSmokeReportDirectory -PathType Container)) {
        throw 'live smoke report directory does not exist'
    }
    $reportFileName = "live-smoke-$candidateRevisionSuffix-$Phase.json"
    $reportPath = Join-Path $liveSmokeReportDirectory $reportFileName
    if (Test-Path -LiteralPath $reportPath) {
        throw 'unique live smoke report path already exists'
    }
    $script:LiveSmokeReportPath = $reportPath
    $script:LiveSmokeReportRelativePath = "artifacts/rag-demo/$reportFileName"
    $script:LiveSmokeExpectedBaseUrl = $BaseUrl
    $smokeText = Invoke-Local -Executable 'uv' -Arguments @(
        'run', 'python', (Join-Path $repoRoot 'scripts/gcp/live-smoke.py'),
        '--base-url', $BaseUrl,
        '--expected-revision', $ExpectedRevision,
        '--expected-image-digest', $ExpectedImageDigest,
        '--manifest', $ManifestPath,
        '--public-access',
        '--output', $script:LiveSmokeReportPath
    ) -WorkingDirectory $repoRoot `
        -TimeoutSeconds 1800
    $smoke = ConvertFrom-ExactJsonObject `
        -Text $smokeText `
        -Context 'public live smoke report'
    if (
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $smoke -Name 'schema_version') `
            -Expected 'rag-demo-live-smoke/v1') -or
        (Get-ObjectProperty -Object $smoke -Name 'passed') -isnot [bool] -or
        (Get-ObjectProperty -Object $smoke -Name 'passed') -ne $true -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $smoke -Name 'base_url') `
            -Expected $BaseUrl) -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $smoke -Name 'access_mode') `
            -Expected 'public_tech_demo')
    ) {
        throw 'public live smoke did not pass the exact deployment identity'
    }
}

function Get-ExactGitCommit {
    $commit = (Invoke-Local -Executable 'git' -Arguments @(
        'rev-parse', '--verify', 'HEAD^{commit}'
    ) -WorkingDirectory $repoRoot).Trim()
    if ($commit -cnotmatch '^(?:[0-9a-f]{40}|[0-9a-f]{64})$') {
        throw 'git did not return one full immutable commit identity'
    }
    return $commit
}

function Assert-GitSourcePinned {
    param([Parameter(Mandatory = $true)][string]$ExpectedCommit)
    $status = Invoke-Local -Executable 'git' -Arguments @(
        'status', '--porcelain', '--untracked-files=all'
    ) -WorkingDirectory $repoRoot
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        throw 'deployment source changed after its immutable commit was captured'
    }
    if ((Get-ExactGitCommit) -cne $ExpectedCommit) {
        throw 'deployment source commit changed after its immutable commit was captured'
    }
}

function Assert-TemporaryBuildRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $temporaryBase = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetTempPath()
    ).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $item = Get-Item -LiteralPath $resolved -Force
    if (
        -not $item.PSIsContainer -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        [System.IO.Path]::GetDirectoryName($resolved) -ne $temporaryBase -or
        [System.IO.Path]::GetFileName($resolved) -notmatch '^hk-rag-build-[0-9a-f]{32}$'
    ) {
        throw 'temporary Cloud Build root failed its exact safety boundary'
    }
    return $resolved
}

function New-TemporaryBuildRoot {
    $temporaryBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $candidate = Join-Path $temporaryBase "hk-rag-build-$([Guid]::NewGuid().ToString('N'))"
    if (Test-Path -LiteralPath $candidate) {
        throw 'temporary Cloud Build root unexpectedly already exists'
    }
    $null = New-Item -ItemType Directory -Path $candidate
    return Assert-TemporaryBuildRoot -Path $candidate
}

function Assert-CommittedBuildContext {
    param([Parameter(Mandatory = $true)][string]$ContextPath)
    $resolvedContext = (Resolve-Path -LiteralPath $ContextPath).Path
    if (Test-Path -LiteralPath (Join-Path $resolvedContext '.git')) {
        throw 'committed Cloud Build context contains Git control data'
    }
    foreach ($entry in Get-ChildItem -LiteralPath $resolvedContext -Recurse -Force) {
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'committed Cloud Build context contains a reparse entry'
        }
        $relative = [System.IO.Path]::GetRelativePath(
            $resolvedContext, $entry.FullName
        ).Replace([System.IO.Path]::DirectorySeparatorChar, '/')
        $segments = @($relative -split '/')
        if (
            -not $entry.PSIsContainer -and
            (
                $entry.Name -eq '.env' -or
                $entry.Extension -eq '.pem' -or
                $entry.Name -match '^(?:key|.*_key)\.json$' -or
                $segments -contains 'credentials'
            )
        ) {
            throw 'committed Cloud Build context contains a forbidden credential path'
        }
    }
    foreach ($requiredPath in @('Dockerfile', 'pyproject.toml', 'uv.lock', 'src')) {
        if (-not (Test-Path -LiteralPath (Join-Path $resolvedContext $requiredPath))) {
            throw 'committed Cloud Build context is missing an image input'
        }
    }
}

function Remove-TemporaryBuildRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $validated = Assert-TemporaryBuildRoot -Path $Path
    Remove-Item -LiteralPath $validated -Recurse -Force
}

function Get-ObjectProperty {
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    # Preserve a nested JSON array as an array even when it has one element.
    # PowerShell otherwise enumerates function output and silently collapses it.
    return ,$property.Value
}

function Test-ObjectProperty {
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    return $null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]
}

function Test-ExactCloudRunTrafficPercent {
    param(
        [AllowNull()][object]$Entry,
        [Parameter(Mandatory = $true)][ValidateSet(0, 100)][int]$Expected
    )
    if ($Entry -isnot [pscustomobject]) {
        return $false
    }
    $property = $Entry.PSObject.Properties['percent']
    if ($null -eq $property) {
        # TrafficTarget.percent is a proto scalar. Cloud Run may omit its
        # canonical default zero, but omission can never represent 100%.
        return $Expected -eq 0
    }
    $actual = $property.Value
    return $actual -is [long] -and $actual -eq $Expected
}

function Test-ExactString {
    param(
        [AllowNull()][object]$Actual,
        [Parameter(Mandatory = $true)][string]$Expected
    )
    return (
        $Actual -is [string] -and
        [string]::Equals(
            [string]$Actual,
            $Expected,
            [System.StringComparison]::Ordinal
        )
    )
}

function Assert-ExactObjectProperties {
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory = $true)][string[]]$Names,
        [Parameter(Mandatory = $true)][string]$Context
    )
    if ($Object -isnot [pscustomobject]) {
        throw "$Context is not one exact JSON object"
    }
    $properties = @($Object.PSObject.Properties)
    if ($properties.Count -ne $Names.Count) {
        throw "$Context has unexpected properties"
    }
    $expected = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($name in $Names) {
        $null = $expected.Add($name)
    }
    foreach ($property in $properties) {
        if (-not $expected.Contains([string]$property.Name)) {
            throw "$Context has unexpected properties"
        }
    }
}

function ConvertFrom-ExactJsonObject {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Context
    )
    if ([string]::IsNullOrWhiteSpace($Text)) {
        throw "$Context returned no JSON object"
    }
    $document = [System.Text.Json.JsonDocument]::Parse($Text)
    try {
        if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) {
            throw "$Context did not return one exact JSON object"
        }
    }
    finally {
        $document.Dispose()
    }
    $value = ConvertFrom-Json -InputObject $Text -NoEnumerate
    if ($null -eq $value -or $value -isnot [pscustomobject]) {
        throw "$Context did not return one exact JSON object"
    }
    return $value
}

function Test-CurrentLiveSmokeFailureReport {
    if (
        [string]::IsNullOrWhiteSpace($script:LiveSmokeReportPath) -or
        [string]::IsNullOrWhiteSpace($script:LiveSmokeReportRelativePath) -or
        [string]::IsNullOrWhiteSpace($script:LiveSmokeExpectedBaseUrl) -or
        -not (Test-Path -LiteralPath $script:LiveSmokeReportPath -PathType Leaf)
    ) {
        return $false
    }
    try {
        $reportText = [System.IO.File]::ReadAllText($script:LiveSmokeReportPath)
        if ($reportText.Length -gt 2097152) {
            return $false
        }
        $report = ConvertFrom-ExactJsonObject `
            -Text $reportText `
            -Context 'persisted live smoke failure report'
        Assert-ExactObjectProperties `
            -Object $report `
            -Names @('access_mode', 'base_url', 'checks', 'error', 'passed', 'schema_version') `
            -Context 'persisted live smoke failure report'
        if (
            $report.schema_version -cne 'rag-demo-live-smoke/v1' -or
            $report.base_url -cne $script:LiveSmokeExpectedBaseUrl -or
            $report.access_mode -cne 'public_tech_demo' -or
            $report.passed -isnot [bool] -or
            $report.passed -ne $false -or
            $report.checks -isnot [pscustomobject] -or
            $report.error -isnot [string] -or
            [string]::IsNullOrWhiteSpace($report.error) -or
            $report.error.Length -gt 512 -or
            $report.error -match '[\x00-\x08\x0b\x0c\x0e-\x1f]'
        ) {
            return $false
        }
        return $true
    }
    catch {
        return $false
    }
}

function Get-CloudRunV2AccessToken {
    if ($script:CloudRunV2UsesTestCredential) {
        return 'offline-cloud-run-v2-test-token'
    }
    $result = Invoke-GcloudResult `
        -Arguments @('auth', 'print-access-token', '--quiet') `
        -AllowFailure `
        -TimeoutSeconds 60
    if ($result.ExitCode -ne 0) {
        throw 'Cloud Run v2 authorization failed without exposing credential output'
    }
    $token = $result.Output.Trim()
    if (
        [string]::IsNullOrWhiteSpace($token) -or
        $token.Length -gt 4096 -or
        $token -match '[\s\x00-\x1f]'
    ) {
        throw 'Cloud Run v2 authorization returned a malformed in-memory credential'
    }
    return $token
}

function Invoke-CloudRunV2Request {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('GET', 'PATCH')]
        [string]$Method,
        [Parameter(Mandatory = $true)][string]$PathAndQuery,
        [AllowNull()][object]$Body = $null
    )
    if (
        -not $PathAndQuery.StartsWith('/v2/', [System.StringComparison]::Ordinal) -or
        $PathAndQuery.Contains('#')
    ) {
        throw 'Cloud Run v2 request path is outside the fixed API boundary'
    }
    $requestUri = [System.Uri]::new("$($script:CloudRunV2Origin)$PathAndQuery")
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler, $true)
    $client.Timeout = [System.TimeSpan]::FromSeconds(60)
    $request = $null
    $response = $null
    $token = $null
    try {
        $token = Get-CloudRunV2AccessToken
        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::new($Method),
            $requestUri
        )
        $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new(
            'Bearer',
            $token
        )
        $request.Headers.Accept.Add(
            [System.Net.Http.Headers.MediaTypeWithQualityHeaderValue]::new('application/json')
        )
        if ($Method -ceq 'PATCH') {
            if ($null -eq $Body) {
                throw 'Cloud Run v2 PATCH body is missing'
            }
            $json = ConvertTo-Json -InputObject $Body -Depth 8 -Compress
            $request.Content = [System.Net.Http.StringContent]::new(
                $json,
                [System.Text.Encoding]::UTF8,
                'application/json'
            )
        }
        elseif ($null -ne $Body) {
            throw 'Cloud Run v2 GET cannot carry a request body'
        }
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw 'Cloud Run v2 returned a non-success status'
        }
        $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return ConvertFrom-ExactJsonObject -Text $text -Context 'Cloud Run v2'
    }
    catch {
        throw 'Cloud Run v2 request failed without exposing authorization or response data'
    }
    finally {
        $token = $null
        if ($null -ne $response) {
            $response.Dispose()
        }
        if ($null -ne $request) {
            $request.Dispose()
        }
        $client.Dispose()
    }
}

function Assert-CloudRunV2Traffic {
    param(
        [AllowNull()][object]$Traffic,
        [Parameter(Mandatory = $true)][string]$ExpectedRevision,
        [AllowEmptyString()][string]$ExpectedUrl = '',
        [Parameter(Mandatory = $true)][string]$Context
    )
    if ($Traffic -isnot [System.Array] -or $Traffic.Count -ne 1) {
        throw "$Context is not one exact JSON traffic array"
    }
    $entry = $Traffic[0]
    if (
        $entry -isnot [pscustomobject] -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $entry -Name 'type') `
            -Expected 'TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION') -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $entry -Name 'revision') `
            -Expected $ExpectedRevision) -or
        (Get-ObjectProperty -Object $entry -Name 'percent') -isnot [long] -or
        (Get-ObjectProperty -Object $entry -Name 'percent') -ne 100
    ) {
        throw "$Context is not one exact pinned numeric 100-percent revision"
    }
    if (
        (Test-ObjectProperty -Object $entry -Name 'tag') -and
        $null -ne (Get-ObjectProperty -Object $entry -Name 'tag')
    ) {
        throw "$Context contains a traffic tag"
    }
    if (
        -not [string]::IsNullOrEmpty($ExpectedUrl) -and
        (Test-ObjectProperty -Object $entry -Name 'uri') -and
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $entry -Name 'uri') `
            -Expected $ExpectedUrl)
    ) {
        throw "$Context does not match the exact stable service URL"
    }
}

function Assert-CloudRunV2ServiceState {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Description,
        [Parameter(Mandatory = $true)][string]$ExpectedTrafficRevision,
        [Parameter(Mandatory = $true)][string]$ExpectedLatestCreatedRevision,
        [Parameter(Mandatory = $true)][string[]]$ExpectedLatestReadyRevisions,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl,
        [AllowEmptyString()][string]$ExpectedEtag = ''
    )
    $serviceName = "projects/$ProjectId/locations/$Region/services/$service"
    $latestCreatedRevisionName = "$serviceName/revisions/$ExpectedLatestCreatedRevision"
    $latestReadyRevision = Get-ObjectProperty `
        -Object $Description `
        -Name 'latestReadyRevision'
    $latestReadyMatches = $false
    foreach ($expectedLatestReadyRevision in $ExpectedLatestReadyRevisions) {
        if (
            Test-ExactString `
                -Actual $latestReadyRevision `
                -Expected "$serviceName/revisions/$expectedLatestReadyRevision"
        ) {
            $latestReadyMatches = $true
            break
        }
    }
    $generation = Get-ObjectProperty -Object $Description -Name 'generation'
    $observedGeneration = Get-ObjectProperty -Object $Description -Name 'observedGeneration'
    $etag = Get-ObjectProperty -Object $Description -Name 'etag'
    if (
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $Description -Name 'name') `
            -Expected $serviceName) -or
        $generation -isnot [string] -or
        $generation -cnotmatch '^[1-9][0-9]*$' -or
        -not (Test-ExactString -Actual $observedGeneration -Expected $generation) -or
        $etag -isnot [string] -or
        [string]::IsNullOrWhiteSpace($etag) -or
        $etag.Length -gt 4096 -or
        $etag -match '[\s\x00-\x1f]' -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $Description -Name 'uri') `
            -Expected $ExpectedUrl) -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $Description -Name 'latestCreatedRevision') `
            -Expected $latestCreatedRevisionName) -or
        -not $latestReadyMatches
    ) {
        throw 'Cloud Run v2 service identity, generation, URL, revision, or etag is not exact'
    }
    if (
        -not [string]::IsNullOrEmpty($ExpectedEtag) -and
        -not (Test-ExactString -Actual $etag -Expected $ExpectedEtag)
    ) {
        throw 'Cloud Run v2 service etag changed across the owned traffic boundary'
    }
    if (Test-ObjectProperty -Object $Description -Name 'reconciling') {
        $reconciling = Get-ObjectProperty -Object $Description -Name 'reconciling'
        if ($reconciling -isnot [bool] -or $reconciling) {
            throw 'Cloud Run v2 service is reconciling or has malformed reconciliation state'
        }
    }
    $terminalCondition = Get-ObjectProperty -Object $Description -Name 'terminalCondition'
    if (
        $terminalCondition -isnot [pscustomobject] -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $terminalCondition -Name 'state') `
            -Expected 'CONDITION_SUCCEEDED')
    ) {
        throw 'Cloud Run v2 service terminal condition did not succeed'
    }
    Assert-CloudRunV2Traffic `
        -Traffic (Get-ObjectProperty -Object $Description -Name 'traffic') `
        -ExpectedRevision $ExpectedTrafficRevision `
        -Context 'Cloud Run v2 desired traffic'
    Assert-CloudRunV2Traffic `
        -Traffic (Get-ObjectProperty -Object $Description -Name 'trafficStatuses') `
        -ExpectedRevision $ExpectedTrafficRevision `
        -ExpectedUrl $ExpectedUrl `
        -Context 'Cloud Run v2 observed traffic'
    return [pscustomobject]@{
        Etag = [string]$etag
        Generation = [string]$generation
    }
}

function Get-CloudRunV2ServiceState {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedTrafficRevision,
        [Parameter(Mandatory = $true)][string]$ExpectedLatestCreatedRevision,
        [Parameter(Mandatory = $true)][string[]]$ExpectedLatestReadyRevisions,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl,
        [AllowEmptyString()][string]$ExpectedEtag = ''
    )
    $description = Invoke-CloudRunV2Request `
        -Method 'GET' `
        -PathAndQuery "/v2/projects/$ProjectId/locations/$Region/services/$service"
    return Assert-CloudRunV2ServiceState `
        -Description $description `
        -ExpectedTrafficRevision $ExpectedTrafficRevision `
        -ExpectedLatestCreatedRevision $ExpectedLatestCreatedRevision `
        -ExpectedLatestReadyRevisions $ExpectedLatestReadyRevisions `
        -ExpectedUrl $ExpectedUrl `
        -ExpectedEtag $ExpectedEtag
}

function Wait-CloudRunV2Operation {
    param([Parameter(Mandatory = $true)][pscustomobject]$InitialOperation)
    $operation = $InitialOperation
    $expectedName = $null
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $name = Get-ObjectProperty -Object $operation -Name 'name'
        $pattern = (
            '^projects/' + [regex]::Escape($ProjectId) +
            '/locations/' + [regex]::Escape($Region) +
            '/operations/[A-Za-z0-9_-]{1,256}$'
        )
        if ($name -isnot [string] -or $name -cnotmatch $pattern) {
            throw 'Cloud Run v2 operation identity is outside the exact project and region'
        }
        if ($null -eq $expectedName) {
            $expectedName = [string]$name
        }
        elseif (-not (Test-ExactString -Actual $name -Expected $expectedName)) {
            throw 'Cloud Run v2 operation identity changed while polling'
        }
        $hasDone = Test-ObjectProperty -Object $operation -Name 'done'
        $done = if ($hasDone) {
            Get-ObjectProperty -Object $operation -Name 'done'
        }
        else {
            $false
        }
        if ($done -isnot [bool]) {
            throw 'Cloud Run v2 operation completion state is malformed'
        }
        $hasError = Test-ObjectProperty -Object $operation -Name 'error'
        $hasResponse = Test-ObjectProperty -Object $operation -Name 'response'
        if ($done) {
            if ($hasError -or -not $hasResponse) {
                throw 'Cloud Run v2 operation failed without exposing response data'
            }
            $response = Get-ObjectProperty -Object $operation -Name 'response'
            if ($response -isnot [pscustomobject]) {
                throw 'Cloud Run v2 operation response is not one exact service object'
            }
            return $response
        }
        if ($hasError -or $hasResponse) {
            throw 'Cloud Run v2 pending operation contains a terminal result'
        }
        if ($stopwatch.Elapsed.TotalSeconds -ge $cloudRunV2DeadlineSeconds) {
            throw 'Cloud Run v2 operation exceeded the controlled polling deadline'
        }
        Start-Sleep -Milliseconds $cloudRunV2PollMilliseconds
        $operation = Invoke-CloudRunV2Request `
            -Method 'GET' `
            -PathAndQuery "/v2/$expectedName"
    }
}

function Invoke-CloudRunV2TrafficCas {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedEtag,
        [Parameter(Mandatory = $true)][string]$TargetRevision,
        [Parameter(Mandatory = $true)][string]$ExpectedLatestCreatedRevision,
        [Parameter(Mandatory = $true)][string[]]$ExpectedLatestReadyRevisions,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl
    )
    $revisionPattern = '^' + [regex]::Escape($service) + '-[0-9]{5}-[a-z0-9][a-z0-9-]*$'
    if (
        [string]::IsNullOrWhiteSpace($ExpectedEtag) -or
        $TargetRevision -cnotmatch $revisionPattern
    ) {
        throw 'Cloud Run v2 traffic CAS input is outside the exact service scope'
    }
    $serviceName = "projects/$ProjectId/locations/$Region/services/$service"
    $body = [ordered]@{
        name = $serviceName
        etag = $ExpectedEtag
        traffic = @(
            [ordered]@{
                type = 'TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION'
                revision = $TargetRevision
                percent = 100
            }
        )
    }
    $operation = Invoke-CloudRunV2Request `
        -Method 'PATCH' `
        -PathAndQuery (
            "/v2/$serviceName" +
            '?updateMask=traffic&allowMissing=false&forceNewRevision=false'
        ) `
        -Body $body
    $response = Wait-CloudRunV2Operation -InitialOperation $operation
    return Assert-CloudRunV2ServiceState `
        -Description $response `
        -ExpectedTrafficRevision $TargetRevision `
        -ExpectedLatestCreatedRevision $ExpectedLatestCreatedRevision `
        -ExpectedLatestReadyRevisions $ExpectedLatestReadyRevisions `
        -ExpectedUrl $ExpectedUrl
}

function Invoke-CloudRunV2TaggedTrafficCas {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedEtag,
        [AllowEmptyString()][string]$StableRevision,
        [Parameter(Mandatory = $true)][string]$CandidateRevision,
        [Parameter(Mandatory = $true)][string]$CandidateTag,
        [Parameter(Mandatory = $true)][ValidateSet(0, 100)][int]$CandidatePercent,
        [Parameter(Mandatory = $true)][string]$CandidateUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedLatestCreatedRevision,
        [Parameter(Mandatory = $true)][string[]]$ExpectedLatestReadyRevisions,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl
    )
    $revisionPattern = '^' + [regex]::Escape($service) + '-[0-9]{5}-[a-z0-9][a-z0-9-]*$'
    if (
        [string]::IsNullOrWhiteSpace($ExpectedEtag) -or
        $CandidateRevision -cnotmatch $revisionPattern -or
        (
            -not [string]::IsNullOrEmpty($StableRevision) -and
            $StableRevision -cnotmatch $revisionPattern
        ) -or
        $CandidateTag -cnotmatch '^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])$'
    ) {
        throw 'Cloud Run v2 tagged traffic CAS input is outside the exact service scope'
    }
    $traffic = @()
    if (-not [string]::IsNullOrEmpty($StableRevision)) {
        $traffic += [ordered]@{
            type = 'TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION'
            revision = $StableRevision
            percent = 100
        }
    }
    $traffic += [ordered]@{
        type = 'TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION'
        revision = $CandidateRevision
        percent = $CandidatePercent
        tag = $CandidateTag
    }
    $serviceName = "projects/$ProjectId/locations/$Region/services/$service"
    $body = [ordered]@{
        name = $serviceName
        etag = $ExpectedEtag
        traffic = $traffic
    }
    $operation = Invoke-CloudRunV2Request `
        -Method 'PATCH' `
        -PathAndQuery (
            "/v2/$serviceName" +
            '?updateMask=traffic&allowMissing=false&forceNewRevision=false'
        ) `
        -Body $body
    $response = Wait-CloudRunV2Operation -InitialOperation $operation
    return Assert-CloudRunV2TaggedState `
        -Description $response `
        -StableRevision $StableRevision `
        -CandidateRevision $CandidateRevision `
        -CandidateTag $CandidateTag `
        -CandidatePercent $CandidatePercent `
        -CandidateUrl $CandidateUrl `
        -ExpectedLatestCreatedRevision $ExpectedLatestCreatedRevision `
        -ExpectedLatestReadyRevisions $ExpectedLatestReadyRevisions `
        -ExpectedUrl $ExpectedUrl
}

function Invoke-CloudRunV2TaggedPromotionCas {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedEtag,
        [Parameter(Mandatory = $true)][string]$CandidateRevision,
        [Parameter(Mandatory = $true)][string]$CandidateTag,
        [Parameter(Mandatory = $true)][string]$CandidateUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl
    )
    return Invoke-CloudRunV2TaggedTrafficCas `
        -ExpectedEtag $ExpectedEtag `
        -StableRevision '' `
        -CandidateRevision $CandidateRevision `
        -CandidateTag $CandidateTag `
        -CandidatePercent 100 `
        -CandidateUrl $CandidateUrl `
        -ExpectedLatestCreatedRevision $CandidateRevision `
        -ExpectedLatestReadyRevisions @($CandidateRevision) `
        -ExpectedUrl $ExpectedUrl
}

function Invoke-CloudRunV2TaggedRollbackCas {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedEtag,
        [Parameter(Mandatory = $true)][string]$StableRevision,
        [Parameter(Mandatory = $true)][string]$CandidateRevision,
        [Parameter(Mandatory = $true)][string]$CandidateTag,
        [Parameter(Mandatory = $true)][string]$CandidateUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl
    )
    return Invoke-CloudRunV2TaggedTrafficCas `
        -ExpectedEtag $ExpectedEtag `
        -StableRevision $StableRevision `
        -CandidateRevision $CandidateRevision `
        -CandidateTag $CandidateTag `
        -CandidatePercent 0 `
        -CandidateUrl $CandidateUrl `
        -ExpectedLatestCreatedRevision $CandidateRevision `
        -ExpectedLatestReadyRevisions @($StableRevision, $CandidateRevision) `
        -ExpectedUrl $ExpectedUrl
}

function Invoke-CloudRunV2TagCleanupCas {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedEtag,
        [Parameter(Mandatory = $true)][string]$TargetRevision,
        [Parameter(Mandatory = $true)][string]$CandidateRevision,
        [Parameter(Mandatory = $true)][string[]]$ExpectedLatestReadyRevisions,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl
    )
    return Invoke-CloudRunV2TrafficCas `
        -ExpectedEtag $ExpectedEtag `
        -TargetRevision $TargetRevision `
        -ExpectedLatestCreatedRevision $CandidateRevision `
        -ExpectedLatestReadyRevisions $ExpectedLatestReadyRevisions `
        -ExpectedUrl $ExpectedUrl
}

function Get-EnabledSecretVersion {
    param([Parameter(Mandatory = $true)][string]$Name)
    $versions = @(Invoke-GcloudArray -Arguments @(
        'secrets', 'versions', 'list', $Name,
        '--filter=state=ENABLED',
        '--limit=2',
        "--project=$ProjectId"
    ))
    if ($versions.Count -ne 1) {
        throw 'deployment secret must have exactly one enabled numeric version'
    }
    $state = Get-ObjectProperty -Object $versions[0] -Name 'state'
    $versionName = Get-ObjectProperty -Object $versions[0] -Name 'name'
    $prefix = "projects/$projectNumber/secrets/$Name/versions/"
    if (
        $state -cne 'ENABLED' -or
        $versionName -isnot [string] -or
        -not $versionName.StartsWith($prefix, [System.StringComparison]::Ordinal)
    ) {
        throw 'deployment secret enabled version metadata is invalid'
    }
    $version = $versionName.Substring($prefix.Length)
    if ($version -cnotmatch '^[1-9][0-9]*$') {
        throw 'deployment secret enabled version is not numeric'
    }
    return $version
}

function Assert-ObservedResource {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Description,
        [Parameter(Mandatory = $true)][string]$ExpectedName,
        [Parameter(Mandatory = $true)][string]$Kind
    )
    $metadata = Get-ObjectProperty -Object $Description -Name 'metadata'
    $status = Get-ObjectProperty -Object $Description -Name 'status'
    $name = Get-ObjectProperty -Object $metadata -Name 'name'
    $generation = Get-ObjectProperty -Object $metadata -Name 'generation'
    $observedGeneration = Get-ObjectProperty -Object $status -Name 'observedGeneration'
    if (
        $metadata -isnot [pscustomobject] -or
        $status -isnot [pscustomobject] -or
        -not (Test-ExactString -Actual $name -Expected $ExpectedName) -or
        $generation -isnot [long] -or
        $observedGeneration -isnot [long] -or
        $generation -lt 1 -or
        $generation -ne $observedGeneration
    ) {
        throw "$Kind identity or observed generation is not exact"
    }
    $conditions = Get-ObjectProperty -Object $status -Name 'conditions'
    if ($conditions -isnot [System.Array]) {
        throw "$Kind conditions are not an exact JSON array"
    }
    $ready = [System.Collections.Generic.List[object]]::new()
    foreach ($condition in $conditions) {
        if ($condition -isnot [pscustomobject]) {
            throw "$Kind conditions contain a non-object entry"
        }
        $conditionType = Get-ObjectProperty -Object $condition -Name 'type'
        if (Test-ExactString -Actual $conditionType -Expected 'Ready') {
            $ready.Add($condition)
        }
    }
    if (
        $ready.Count -ne 1 -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $ready[0] -Name 'status') `
            -Expected 'True')
    ) {
        throw "$Kind is not exactly Ready at the observed generation"
    }
    return [pscustomobject]@{
        Metadata = $metadata
        Status = $status
    }
}

function Assert-ServiceObservation {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Description,
        [AllowEmptyString()][string]$ExpectedUrl = ''
    )
    $observation = Assert-ObservedResource `
        -Description $Description `
        -ExpectedName $service `
        -Kind 'Cloud Run service'
    $annotations = Get-ObjectProperty -Object $observation.Metadata -Name 'annotations'
    $invokerIamDisabled = Get-ObjectProperty `
        -Object $annotations `
        -Name 'run.googleapis.com/invoker-iam-disabled'
    $url = Get-ObjectProperty -Object $observation.Status -Name 'url'
    if (-not (Test-ExactString -Actual $invokerIamDisabled -Expected 'true')) {
        throw 'Cloud Run service invoker IAM check is not disabled'
    }
    $parsedUrl = $null
    if (
        $url -isnot [string] -or
        -not [System.Uri]::TryCreate(
            [string]$url,
            [System.UriKind]::Absolute,
            [ref]$parsedUrl
        ) -or
        -not [string]::Equals(
            $parsedUrl.Scheme,
            [System.Uri]::UriSchemeHttps,
            [System.StringComparison]::Ordinal
        ) -or
        $parsedUrl.HostNameType -ne [System.UriHostNameType]::Dns -or
        -not $parsedUrl.DnsSafeHost.EndsWith(
            '.run.app',
            [System.StringComparison]::Ordinal
        ) -or
        -not $parsedUrl.IsDefaultPort -or
        -not [string]::IsNullOrEmpty($parsedUrl.UserInfo) -or
        -not [string]::IsNullOrEmpty($parsedUrl.Query) -or
        -not [string]::IsNullOrEmpty($parsedUrl.Fragment) -or
        $parsedUrl.AbsolutePath -cne '/'
    ) {
        throw 'Cloud Run service does not have an exact HTTPS URL'
    }
    if (
        -not [string]::IsNullOrEmpty($ExpectedUrl) -and
        -not (Test-ExactString -Actual $url -Expected $ExpectedUrl)
    ) {
        throw 'Cloud Run service URL changed across candidate promotion'
    }
    return [pscustomobject]@{
        Metadata = $observation.Metadata
        Status = $observation.Status
        Url = $url
    }
}

function Get-ServiceRevisionName {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Status,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $revision = Get-ObjectProperty -Object $Status -Name $Name
    $pattern = '^' + [regex]::Escape($service) + '-[0-9]{5}-[a-z0-9][a-z0-9-]*$'
    if ($revision -isnot [string] -or $revision -cnotmatch $pattern) {
        throw "Cloud Run service $Name is outside the exact service revision scope"
    }
    return $revision
}

function Assert-RunAppUrl {
    param(
        [AllowNull()][object]$Value,
        [Parameter(Mandatory = $true)][string]$Context
    )
    $parsed = $null
    if (
        $Value -isnot [string] -or
        -not [System.Uri]::TryCreate(
            [string]$Value,
            [System.UriKind]::Absolute,
            [ref]$parsed
        ) -or
        $parsed.Scheme -cne [System.Uri]::UriSchemeHttps -or
        $parsed.HostNameType -ne [System.UriHostNameType]::Dns -or
        -not $parsed.DnsSafeHost.EndsWith(
            '.run.app',
            [System.StringComparison]::Ordinal
        ) -or
        -not $parsed.IsDefaultPort -or
        -not [string]::IsNullOrEmpty($parsed.UserInfo) -or
        -not [string]::IsNullOrEmpty($parsed.Query) -or
        -not [string]::IsNullOrEmpty($parsed.Fragment) -or
        $parsed.AbsolutePath -cne '/'
    ) {
        throw "$Context is not one exact HTTPS run.app origin"
    }
    return [string]$Value
}

function Get-CloudRunV1TrafficSnapshot {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Status,
        [Parameter(Mandatory = $true)][string]$ExpectedCandidateTag
    )
    $traffic = Get-ObjectProperty -Object $Status -Name 'traffic'
    if (
        $traffic -isnot [System.Array] -or
        $traffic.Count -lt 1 -or
        $traffic.Count -gt 2
    ) {
        throw 'Cloud Run service traffic is outside the stable/tagged-candidate contract'
    }
    $revisionPattern = '^' + [regex]::Escape($service) + '-[0-9]{5}-[a-z0-9][a-z0-9-]*$'
    $stable = @($traffic | Where-Object {
        $entryTag = Get-ObjectProperty -Object $_ -Name 'tag'
        $null -eq $entryTag
    })
    $tagged = @($traffic | Where-Object {
        Test-ExactString `
            -Actual (Get-ObjectProperty -Object $_ -Name 'tag') `
            -Expected $ExpectedCandidateTag
    })
    if ($stable.Count -ne 1 -or $tagged.Count -ne ($traffic.Count - 1)) {
        throw 'Cloud Run service does not contain one exact stable target and optional candidate tag'
    }
    $stableRevision = Get-ObjectProperty -Object $stable[0] -Name 'revisionName'
    if (
        $stableRevision -isnot [string] -or
        $stableRevision -cnotmatch $revisionPattern -or
        (Get-ObjectProperty -Object $stable[0] -Name 'percent') -isnot [long] -or
        (Get-ObjectProperty -Object $stable[0] -Name 'percent') -ne 100
    ) {
        throw 'Cloud Run stable traffic target is not an exact pinned revision'
    }
    if (Test-ObjectProperty -Object $stable[0] -Name 'latestRevision') {
        $stableLatest = Get-ObjectProperty -Object $stable[0] -Name 'latestRevision'
        if (
            $null -ne $stableLatest -and
            ($stableLatest -isnot [bool] -or $stableLatest)
        ) {
            throw 'Cloud Run stable traffic target is floating or malformed'
        }
    }
    if ($tagged.Count -eq 0) {
        return [pscustomobject]@{
            StableRevision = [string]$stableRevision
            CandidateRevision = ''
            CandidateUrl = ''
        }
    }
    $candidateRevision = Get-ObjectProperty -Object $tagged[0] -Name 'revisionName'
    $candidateUrl = Get-ObjectProperty -Object $tagged[0] -Name 'url'
    if (
        $candidateRevision -isnot [string] -or
        $candidateRevision -cnotmatch $revisionPattern -or
        $candidateRevision -ceq $stableRevision -or
        -not (Test-ExactCloudRunTrafficPercent -Entry $tagged[0] -Expected 0)
    ) {
        throw 'Cloud Run candidate tag is not one distinct zero-traffic revision'
    }
    if (Test-ObjectProperty -Object $tagged[0] -Name 'latestRevision') {
        $candidateLatest = Get-ObjectProperty -Object $tagged[0] -Name 'latestRevision'
        if (
            $null -ne $candidateLatest -and
            ($candidateLatest -isnot [bool] -or $candidateLatest)
        ) {
            throw 'Cloud Run candidate traffic target is floating or malformed'
        }
    }
    return [pscustomobject]@{
        StableRevision = [string]$stableRevision
        CandidateRevision = [string]$candidateRevision
        CandidateUrl = Assert-RunAppUrl `
            -Value $candidateUrl `
            -Context 'Cloud Run candidate tag URL'
    }
}

function Assert-CloudRunV2TaggedState {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Description,
        [AllowEmptyString()][string]$StableRevision,
        [Parameter(Mandatory = $true)][string]$CandidateRevision,
        [Parameter(Mandatory = $true)][string]$CandidateTag,
        [Parameter(Mandatory = $true)][ValidateSet(0, 100)][int]$CandidatePercent,
        [Parameter(Mandatory = $true)][string]$CandidateUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedLatestCreatedRevision,
        [Parameter(Mandatory = $true)][string[]]$ExpectedLatestReadyRevisions,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl,
        [AllowEmptyString()][string]$ExpectedEtag = ''
    )
    $serviceName = "projects/$ProjectId/locations/$Region/services/$service"
    $latestReadyRevision = Get-ObjectProperty -Object $Description -Name 'latestReadyRevision'
    $latestReadyMatches = $false
    foreach ($expectedReady in $ExpectedLatestReadyRevisions) {
        if (
            Test-ExactString `
                -Actual $latestReadyRevision `
                -Expected "$serviceName/revisions/$expectedReady"
        ) {
            $latestReadyMatches = $true
            break
        }
    }
    $generation = Get-ObjectProperty -Object $Description -Name 'generation'
    $observedGeneration = Get-ObjectProperty -Object $Description -Name 'observedGeneration'
    $etag = Get-ObjectProperty -Object $Description -Name 'etag'
    $terminal = Get-ObjectProperty -Object $Description -Name 'terminalCondition'
    if (
        -not (Test-ExactString -Actual (Get-ObjectProperty -Object $Description -Name 'name') -Expected $serviceName) -or
        $generation -isnot [string] -or
        $generation -cnotmatch '^[1-9][0-9]*$' -or
        -not (Test-ExactString -Actual $observedGeneration -Expected $generation) -or
        $etag -isnot [string] -or
        [string]::IsNullOrWhiteSpace($etag) -or
        $etag.Length -gt 4096 -or
        $etag -match '[\s\x00-\x1f]' -or
        -not (Test-ExactString -Actual (Get-ObjectProperty -Object $Description -Name 'uri') -Expected $ExpectedUrl) -or
        -not (Test-ExactString -Actual (Get-ObjectProperty -Object $Description -Name 'latestCreatedRevision') -Expected "$serviceName/revisions/$ExpectedLatestCreatedRevision") -or
        -not $latestReadyMatches -or
        $terminal -isnot [pscustomobject] -or
        -not (Test-ExactString -Actual (Get-ObjectProperty -Object $terminal -Name 'state') -Expected 'CONDITION_SUCCEEDED')
    ) {
        throw 'Cloud Run v2 tagged service identity is not exact'
    }
    if (
        -not [string]::IsNullOrEmpty($ExpectedEtag) -and
        -not (Test-ExactString -Actual $etag -Expected $ExpectedEtag)
    ) {
        throw 'Cloud Run v2 tagged service etag changed across the owned boundary'
    }
    $desired = Get-ObjectProperty -Object $Description -Name 'traffic'
    $observed = Get-ObjectProperty -Object $Description -Name 'trafficStatuses'
    $expectedCount = if ([string]::IsNullOrEmpty($StableRevision)) { 1 } else { 2 }
    if (
        $desired -isnot [System.Array] -or
        $observed -isnot [System.Array] -or
        $desired.Count -ne $expectedCount -or
        $observed.Count -ne $expectedCount
    ) {
        throw 'Cloud Run v2 tagged traffic is not one exact desired/observed pair'
    }
    $trafficSets = @(
        [pscustomobject]@{ Entries = $desired; Observed = $false },
        [pscustomobject]@{ Entries = $observed; Observed = $true }
    )
    foreach ($trafficSet in $trafficSets) {
        $entries = $trafficSet.Entries
        $candidateEntries = @($entries | Where-Object {
            Test-ExactString -Actual (Get-ObjectProperty -Object $_ -Name 'tag') -Expected $CandidateTag
        })
        if ($candidateEntries.Count -ne 1) {
            throw 'Cloud Run v2 candidate tag is missing or duplicated'
        }
        $candidate = $candidateEntries[0]
        if (
            -not (Test-ExactString -Actual (Get-ObjectProperty -Object $candidate -Name 'type') -Expected 'TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION') -or
            -not (Test-ExactString -Actual (Get-ObjectProperty -Object $candidate -Name 'revision') -Expected $CandidateRevision) -or
            -not (Test-ExactCloudRunTrafficPercent -Entry $candidate -Expected $CandidatePercent)
        ) {
            throw 'Cloud Run v2 tagged candidate target is not exact'
        }
        if ($trafficSet.Observed) {
            if (
                -not (Test-ExactString `
                    -Actual (Get-ObjectProperty -Object $candidate -Name 'uri') `
                    -Expected $CandidateUrl)
            ) {
                throw 'Cloud Run v2 did not observe the exact candidate tag URL'
            }
        }
        if (-not [string]::IsNullOrEmpty($StableRevision)) {
            $stableEntries = @($entries | Where-Object {
                $null -eq (Get-ObjectProperty -Object $_ -Name 'tag')
            })
            if ($stableEntries.Count -ne 1) {
                throw 'Cloud Run v2 stable target is missing or duplicated'
            }
            $stable = $stableEntries[0]
            if (
                -not (Test-ExactString -Actual (Get-ObjectProperty -Object $stable -Name 'type') -Expected 'TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION') -or
                -not (Test-ExactString -Actual (Get-ObjectProperty -Object $stable -Name 'revision') -Expected $StableRevision) -or
                (Get-ObjectProperty -Object $stable -Name 'percent') -isnot [long] -or
                (Get-ObjectProperty -Object $stable -Name 'percent') -ne 100
            ) {
                throw 'Cloud Run v2 stable target is not exact while candidate is tagged'
            }
        }
    }
    return [pscustomobject]@{
        Etag = [string]$etag
        Generation = [string]$generation
        CandidateUrl = $CandidateUrl
    }
}

function Get-CloudRunV2TaggedState {
    param(
        [AllowEmptyString()][string]$StableRevision,
        [Parameter(Mandatory = $true)][string]$CandidateRevision,
        [Parameter(Mandatory = $true)][string]$CandidateTag,
        [Parameter(Mandatory = $true)][ValidateSet(0, 100)][int]$CandidatePercent,
        [Parameter(Mandatory = $true)][string]$CandidateUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedLatestCreatedRevision,
        [Parameter(Mandatory = $true)][string[]]$ExpectedLatestReadyRevisions,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl,
        [AllowEmptyString()][string]$ExpectedEtag = ''
    )
    $description = Invoke-CloudRunV2Request `
        -Method 'GET' `
        -PathAndQuery "/v2/projects/$ProjectId/locations/$Region/services/$service"
    return Assert-CloudRunV2TaggedState `
        -Description $description `
        -StableRevision $StableRevision `
        -CandidateRevision $CandidateRevision `
        -CandidateTag $CandidateTag `
        -CandidatePercent $CandidatePercent `
        -CandidateUrl $CandidateUrl `
        -ExpectedLatestCreatedRevision $ExpectedLatestCreatedRevision `
        -ExpectedLatestReadyRevisions $ExpectedLatestReadyRevisions `
        -ExpectedUrl $ExpectedUrl `
        -ExpectedEtag $ExpectedEtag
}

function Get-CloudRunV2NewCandidateOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$StableRevision,
        [Parameter(Mandatory = $true)][string]$PriorLatestCreatedRevision,
        [Parameter(Mandatory = $true)][string]$ExpectedCandidateRevision,
        [Parameter(Mandatory = $true)][string]$CandidateTag,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl
    )
    $description = Invoke-CloudRunV2Request `
        -Method 'GET' `
        -PathAndQuery "/v2/projects/$ProjectId/locations/$Region/services/$service"
    $serviceName = "projects/$ProjectId/locations/$Region/services/$service"
    if (
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $description -Name 'name') `
            -Expected $serviceName) -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $description -Name 'uri') `
            -Expected $ExpectedUrl)
    ) {
        throw 'Cloud Run v2 candidate ownership service identity is not exact'
    }
    $desired = Get-ObjectProperty -Object $description -Name 'traffic'
    $observed = Get-ObjectProperty -Object $description -Name 'trafficStatuses'
    $revisionPattern = '^' + [regex]::Escape($service) + '-[0-9]{5}-[a-z0-9][a-z0-9-]*$'
    if (
        $ExpectedCandidateRevision -cnotmatch $revisionPattern -or
        $ExpectedCandidateRevision -ceq $StableRevision
    ) {
        throw 'expected Cloud Run candidate revision is unsafe or not distinct'
    }

    $pending = $false
    $observedCandidate = $null
    foreach ($trafficSet in @(
        [pscustomobject]@{ Entries = $desired; Observed = $false },
        [pscustomobject]@{ Entries = $observed; Observed = $true }
    )) {
        if ($null -eq $trafficSet.Entries) {
            $pending = $true
            continue
        }
        if ($trafficSet.Entries -isnot [System.Array]) {
            throw 'Cloud Run v2 candidate ownership traffic is not an exact array'
        }
        $allTagged = @($trafficSet.Entries | Where-Object {
            $null -ne (Get-ObjectProperty -Object $_ -Name 'tag')
        })
        $expectedTagged = @($allTagged | Where-Object {
            Test-ExactString `
                -Actual (Get-ObjectProperty -Object $_ -Name 'tag') `
                -Expected $CandidateTag
        })
        if ($allTagged.Count -ne $expectedTagged.Count -or $expectedTagged.Count -gt 1) {
            throw 'Cloud Run v2 candidate ownership contains another or duplicate tag'
        }
        if ($expectedTagged.Count -eq 0) {
            $pending = $true
        }
        else {
            $candidate = $expectedTagged[0]
            if (
                -not (Test-ExactString `
                    -Actual (Get-ObjectProperty -Object $candidate -Name 'revision') `
                    -Expected $ExpectedCandidateRevision) -or
                -not (Test-ExactCloudRunTrafficPercent -Entry $candidate -Expected 0)
            ) {
                throw 'Cloud Run v2 candidate ownership tag targets another revision or percent'
            }
            if ($trafficSet.Observed) {
                $observedCandidate = $candidate
            }
        }
        $stable = @($trafficSet.Entries | Where-Object {
            $null -eq (Get-ObjectProperty -Object $_ -Name 'tag')
        })
        if ($stable.Count -eq 0) {
            $pending = $true
        }
        elseif (
            $stable.Count -ne 1 -or
            -not (Test-ExactString `
                -Actual (Get-ObjectProperty -Object $stable[0] -Name 'revision') `
                -Expected $StableRevision) -or
            (Get-ObjectProperty -Object $stable[0] -Name 'percent') -isnot [long] -or
            (Get-ObjectProperty -Object $stable[0] -Name 'percent') -ne 100
        ) {
            throw 'Cloud Run v2 candidate ownership changed the prior stable target'
        }
    }

    $latestCreated = Get-ObjectProperty -Object $description -Name 'latestCreatedRevision'
    if (
        -not (Test-ExactString `
            -Actual $latestCreated `
            -Expected "$serviceName/revisions/$ExpectedCandidateRevision")
    ) {
        if (Test-ExactString `
            -Actual $latestCreated `
            -Expected "$serviceName/revisions/$PriorLatestCreatedRevision") {
            $pending = $true
        }
        else {
            throw 'Cloud Run v2 candidate ownership latest revision belongs to another actor'
        }
    }
    $generation = Get-ObjectProperty -Object $description -Name 'generation'
    $observedGeneration = Get-ObjectProperty -Object $description -Name 'observedGeneration'
    $terminal = Get-ObjectProperty -Object $description -Name 'terminalCondition'
    $terminalState = Get-ObjectProperty -Object $terminal -Name 'state'
    if (
        $generation -is [string] -and
        $observedGeneration -is [string] -and
        $generation -cne $observedGeneration
    ) {
        $pending = $true
    }
    if ($terminalState -in @('CONDITION_PENDING', 'CONDITION_RECONCILING')) {
        $pending = $true
    }
    elseif (
        $terminalState -is [string] -and
        $terminalState -cne 'CONDITION_SUCCEEDED'
    ) {
        throw 'Cloud Run v2 candidate ownership entered a terminal failure state'
    }
    if ($pending) {
        throw [System.TimeoutException]::new(
            'exact Cloud Run candidate ownership is still pending observation'
        )
    }
    if ($null -eq $observedCandidate) {
        throw 'Cloud Run v2 candidate ownership has no observed exact tag'
    }
    $candidateRevision = $ExpectedCandidateRevision
    $candidateUrl = Assert-RunAppUrl `
        -Value (Get-ObjectProperty -Object $observedCandidate -Name 'uri') `
        -Context 'Cloud Run v2 candidate ownership tag URL'
    if (Test-ExactString -Actual $candidateUrl -Expected $ExpectedUrl) {
        throw 'Cloud Run v2 candidate ownership tag URL is not distinct from the stable URL'
    }
    $state = Assert-CloudRunV2TaggedState `
        -Description $description `
        -StableRevision $StableRevision `
        -CandidateRevision $candidateRevision `
        -CandidateTag $CandidateTag `
        -CandidatePercent 0 `
        -CandidateUrl $candidateUrl `
        -ExpectedLatestCreatedRevision $candidateRevision `
        -ExpectedLatestReadyRevisions @($StableRevision, $candidateRevision) `
        -ExpectedUrl $ExpectedUrl
    return [pscustomobject]@{
        CandidateRevision = [string]$candidateRevision
        CandidateUrl = [string]$candidateUrl
        Etag = [string]$state.Etag
        Generation = [string]$state.Generation
    }
}

function Wait-CloudRunV2NewCandidateOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$StableRevision,
        [Parameter(Mandatory = $true)][string]$PriorLatestCreatedRevision,
        [Parameter(Mandatory = $true)][string]$ExpectedCandidateRevision,
        [Parameter(Mandatory = $true)][string]$CandidateTag,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl
    )
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($cloudRunV2DeadlineSeconds)
    while ($true) {
        try {
            return Get-CloudRunV2NewCandidateOwnership `
                -StableRevision $StableRevision `
                -PriorLatestCreatedRevision $PriorLatestCreatedRevision `
                -ExpectedCandidateRevision $ExpectedCandidateRevision `
                -CandidateTag $CandidateTag `
                -ExpectedUrl $ExpectedUrl
        }
        catch {
            if ($_.Exception -isnot [System.TimeoutException]) {
                throw
            }
            if ([DateTimeOffset]::UtcNow -ge $deadline) {
                throw 'Cloud Run did not expose this apply exact candidate ownership before deadline'
            }
            Start-Sleep -Milliseconds $cloudRunV2PollMilliseconds
        }
    }
}

function Get-ExactPinnedTrafficRevision {
    param([Parameter(Mandatory = $true)][pscustomobject]$Status)
    $traffic = Get-ObjectProperty -Object $Status -Name 'traffic'
    if ($traffic -isnot [System.Array]) {
        throw 'Cloud Run service traffic is not an exact JSON array'
    }
    if ($traffic.Count -ne 1 -or $traffic[0] -isnot [pscustomobject]) {
        throw 'Cloud Run service traffic is not one exact pinned revision'
    }
    $entry = $traffic[0]
    $revision = Get-ObjectProperty -Object $entry -Name 'revisionName'
    $percent = Get-ObjectProperty -Object $entry -Name 'percent'
    $tag = Get-ObjectProperty -Object $entry -Name 'tag'
    if (
        $revision -isnot [string] -or
        $percent -isnot [long] -or
        $percent -ne 100 -or
        $null -ne $tag
    ) {
        throw 'Cloud Run service traffic is not one exact untagged numeric 100-percent revision'
    }
    if (Test-ObjectProperty -Object $entry -Name 'latestRevision') {
        $latestRevision = Get-ObjectProperty -Object $entry -Name 'latestRevision'
        if (
            $null -ne $latestRevision -and
            ($latestRevision -isnot [bool] -or $latestRevision)
        ) {
            throw 'Cloud Run service traffic uses a floating or malformed latest revision target'
        }
    }
    $pattern = '^' + [regex]::Escape($service) + '-[0-9]{5}-[a-z0-9][a-z0-9-]*$'
    if ($revision -cnotmatch $pattern) {
        throw 'Cloud Run service traffic revision is outside the exact service scope'
    }
    return $revision
}

function Assert-AbsentOrEmptyArrayProperty {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Context
    )
    if (-not (Test-ObjectProperty -Object $Object -Name $Name)) {
        return
    }
    $value = Get-ObjectProperty -Object $Object -Name $Name
    if ($value -isnot [System.Array] -or $value.Count -ne 0) {
        throw "$Context must be absent or an empty array"
    }
}

function Assert-CandidateRevision {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Description,
        [Parameter(Mandatory = $true)][string]$Revision,
        [Parameter(Mandatory = $true)][string]$ImmutableImage,
        [Parameter(Mandatory = $true)][string]$InstanceConnection,
        [Parameter(Mandatory = $true)][string]$DbSecretVersion
    )
    $observation = Assert-ObservedResource `
        -Description $Description `
        -ExpectedName $Revision `
        -Kind 'Cloud Run revision'
    $annotations = Get-ObjectProperty -Object $observation.Metadata -Name 'annotations'
    $cloudSqlAnnotation = Get-ObjectProperty `
        -Object $annotations `
        -Name 'run.googleapis.com/cloudsql-instances'
    $maxScaleAnnotation = Get-ObjectProperty `
        -Object $annotations `
        -Name 'autoscaling.knative.dev/maxScale'
    if (
        $annotations -isnot [pscustomobject] -or
        -not (Test-ExactString -Actual $cloudSqlAnnotation -Expected $InstanceConnection) -or
        -not (Test-ExactString -Actual $maxScaleAnnotation -Expected '3') -or
        (Test-ObjectProperty -Object $annotations -Name 'autoscaling.knative.dev/minScale') -or
        (Test-ObjectProperty -Object $annotations -Name 'run.googleapis.com/secrets')
    ) {
        throw 'Cloud Run revision annotations do not match the exact runtime contract'
    }

    $spec = Get-ObjectProperty -Object $Description -Name 'spec'
    $revisionServiceAccount = Get-ObjectProperty -Object $spec -Name 'serviceAccountName'
    if (
        $spec -isnot [pscustomobject] -or
        -not (Test-ExactString -Actual $revisionServiceAccount -Expected $serviceAccount) -or
        (Get-ObjectProperty -Object $spec -Name 'containerConcurrency') -isnot [long] -or
        (Get-ObjectProperty -Object $spec -Name 'containerConcurrency') -ne 20 -or
        (Get-ObjectProperty -Object $spec -Name 'timeoutSeconds') -isnot [long] -or
        (Get-ObjectProperty -Object $spec -Name 'timeoutSeconds') -ne 300
    ) {
        throw 'Cloud Run revision service account, concurrency, or timeout is not exact'
    }
    Assert-AbsentOrEmptyArrayProperty -Object $spec -Name 'volumes' -Context 'revision volumes'
    $containers = Get-ObjectProperty -Object $spec -Name 'containers'
    if ($containers -isnot [System.Array]) {
        throw 'Cloud Run revision containers are not an exact JSON array'
    }
    if ($containers.Count -ne 1 -or $containers[0] -isnot [pscustomobject]) {
        throw 'Cloud Run revision does not have exactly one container'
    }
    $container = $containers[0]
    if (-not (Test-ExactString `
        -Actual (Get-ObjectProperty -Object $container -Name 'image') `
        -Expected $ImmutableImage)) {
        throw 'Cloud Run revision does not use the exact immutable image'
    }
    Assert-AbsentOrEmptyArrayProperty -Object $container -Name 'command' -Context 'container command'
    Assert-AbsentOrEmptyArrayProperty -Object $container -Name 'args' -Context 'container arguments'
    Assert-AbsentOrEmptyArrayProperty `
        -Object $container `
        -Name 'volumeMounts' `
        -Context 'container volume mounts'

    $resources = Get-ObjectProperty -Object $container -Name 'resources'
    Assert-ExactObjectProperties `
        -Object $resources `
        -Names @('limits') `
        -Context 'Cloud Run revision resources'
    $limits = Get-ObjectProperty -Object $resources -Name 'limits'
    Assert-ExactObjectProperties `
        -Object $limits `
        -Names @('cpu', 'memory') `
        -Context 'Cloud Run revision resource limits'
    if (
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $limits -Name 'cpu') `
            -Expected '1') -or
        -not (Test-ExactString `
            -Actual (Get-ObjectProperty -Object $limits -Name 'memory') `
            -Expected '1Gi')
    ) {
        throw 'Cloud Run revision CPU or memory limit is not exact'
    }
    $ports = Get-ObjectProperty -Object $container -Name 'ports'
    if ($ports -isnot [System.Array]) {
        throw 'Cloud Run revision ports are not an exact JSON array'
    }
    if (
        $ports.Count -ne 1 -or
        $ports[0] -isnot [pscustomobject] -or
        (Get-ObjectProperty -Object $ports[0] -Name 'containerPort') -isnot [long] -or
        (Get-ObjectProperty -Object $ports[0] -Name 'containerPort') -ne 8080
    ) {
        throw 'Cloud Run revision port is not the exact 8080 http1 contract'
    }
    Assert-ExactObjectProperties `
        -Object $ports[0] `
        -Names @('containerPort', 'name') `
        -Context 'Cloud Run revision port'
    if (-not (Test-ExactString `
        -Actual (Get-ObjectProperty -Object $ports[0] -Name 'name') `
        -Expected 'http1')) {
        throw 'Cloud Run revision port is not the exact 8080 http1 contract'
    }

    $expectedPlainEnvironment = [ordered]@{
        GOOGLE_CLOUD_PROJECT = $ProjectId
        VERTEX_LOCATION = 'global'
        RAG_RELEASE_ID = $releaseId
        RAG_RELEASE_MANIFEST_SHA256 = $manifestSha
        RAG_EMBEDDING_MODEL = 'gemini-embedding-2'
        RAG_EMBEDDING_DIMENSION = '768'
        RAG_GENERATION_MODEL = 'gemini-3.5-flash-lite'
        RAG_RELEASE_BUCKET = $bucket
        RAG_RELEVANCE_POLICY_SHA256 = $verification.relevance_policy_sha256
        RAG_POSTER_AUTHORITY_SHA256 = $verification.poster_authority_sha256
        RAG_IMAGE_DIGEST = $imageDigest
        DB_NAME = $database
        DB_USER = $databaseUser
        INSTANCE_CONNECTION_NAME = $InstanceConnection
    }
    $expectedSecrets = [ordered]@{
        DB_PASSWORD = [ordered]@{ name = $dbSecret; key = $DbSecretVersion }
    }
    $environment = Get-ObjectProperty -Object $container -Name 'env'
    if ($environment -isnot [System.Array]) {
        throw 'Cloud Run revision environment is not an exact JSON array'
    }
    if ($environment.Count -ne ($expectedPlainEnvironment.Count + $expectedSecrets.Count)) {
        throw 'Cloud Run revision environment does not have the exact variable count'
    }
    $actualEnvironment = [System.Collections.Generic.Dictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in $environment) {
        $name = Get-ObjectProperty -Object $entry -Name 'name'
        if (
            $entry -isnot [pscustomobject] -or
            $name -isnot [string] -or
            [string]::IsNullOrWhiteSpace($name) -or
            $actualEnvironment.ContainsKey($name)
        ) {
            throw 'Cloud Run revision environment contains an invalid or duplicate variable'
        }
        $actualEnvironment.Add($name, $entry)
    }
    foreach ($expected in $expectedPlainEnvironment.GetEnumerator()) {
        if (-not $actualEnvironment.ContainsKey($expected.Key)) {
            throw 'Cloud Run revision is missing a fixed plain environment variable'
        }
        $entry = $actualEnvironment[$expected.Key]
        Assert-ExactObjectProperties `
            -Object $entry `
            -Names @('name', 'value') `
            -Context "Cloud Run revision plain environment $($expected.Key)"
        $actualValue = Get-ObjectProperty -Object $entry -Name 'value'
        if (
            -not (Test-ExactString -Actual $actualValue -Expected ([string]$expected.Value))
        ) {
            throw 'Cloud Run revision plain environment contract drifted'
        }
    }
    foreach ($expected in $expectedSecrets.GetEnumerator()) {
        if (-not $actualEnvironment.ContainsKey($expected.Key)) {
            throw 'Cloud Run revision is missing an exact secret environment variable'
        }
        $entry = $actualEnvironment[$expected.Key]
        Assert-ExactObjectProperties `
            -Object $entry `
            -Names @('name', 'valueFrom') `
            -Context "Cloud Run revision secret environment $($expected.Key)"
        $valueFrom = Get-ObjectProperty -Object $entry -Name 'valueFrom'
        Assert-ExactObjectProperties `
            -Object $valueFrom `
            -Names @('secretKeyRef') `
            -Context "Cloud Run revision secret valueFrom $($expected.Key)"
        $secretKeyRef = Get-ObjectProperty -Object $valueFrom -Name 'secretKeyRef'
        Assert-ExactObjectProperties `
            -Object $secretKeyRef `
            -Names @('name', 'key') `
            -Context "Cloud Run revision secretKeyRef $($expected.Key)"
        $actualSecretName = Get-ObjectProperty -Object $secretKeyRef -Name 'name'
        $actualSecretKey = Get-ObjectProperty -Object $secretKeyRef -Name 'key'
        if (
            -not (Test-ExactString `
                -Actual $actualSecretName `
                -Expected ([string]$expected.Value.name)) -or
            -not (Test-ExactString `
                -Actual $actualSecretKey `
                -Expected ([string]$expected.Value.key))
        ) {
            throw 'Cloud Run revision secret name or numeric version drifted'
        }
    }
}

function ConvertTo-ControlledExecutionId {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$JobName
    )
    $shortPattern = '^' + [regex]::Escape($JobName) + '-[a-z0-9]{5}$'
    if ($Name -cmatch $shortPattern) {
        return $Name
    }

    $fullPrefix = "projects/$ProjectId/locations/$Region/jobs/$JobName/executions/"
    if (-not $Name.StartsWith($fullPrefix, [System.StringComparison]::Ordinal)) {
        throw 'Cloud Run Job returned an execution identity outside the exact job scope'
    }
    $shortName = $Name.Substring($fullPrefix.Length)
    if ($shortName -cnotmatch $shortPattern) {
        throw 'Cloud Run Job returned an invalid execution identity'
    }
    return $shortName
}

function Get-ControlledExecutionId {
    param(
        [Parameter(Mandatory = $true)][object]$Execution,
        [Parameter(Mandatory = $true)][string]$JobName
    )
    $topLevelName = [string](Get-ObjectProperty -Object $Execution -Name 'name')
    $metadata = Get-ObjectProperty -Object $Execution -Name 'metadata'
    $metadataName = [string](Get-ObjectProperty -Object $metadata -Name 'name')
    $names = @(@($topLevelName, $metadataName) | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_)
    })
    if ($names.Count -eq 0) {
        throw 'Cloud Run Job returned an invalid execution identity'
    }

    $executionIds = @($names | ForEach-Object {
        ConvertTo-ControlledExecutionId -Name $_ -JobName $JobName
    } | Select-Object -Unique)
    if ($executionIds.Count -ne 1) {
        throw 'Cloud Run Job returned ambiguous execution identities'
    }
    return [string]$executionIds[0]
}

function Get-ExecutionCompletedStatus {
    param([Parameter(Mandatory = $true)][object]$Execution)
    $completionStatus = [string](Get-ObjectProperty -Object $Execution -Name 'completionStatus')
    if ($completionStatus -eq 'EXECUTION_SUCCEEDED') {
        return 'True'
    }
    if ($completionStatus -in @('EXECUTION_FAILED', 'EXECUTION_CANCELLED')) {
        return 'False'
    }
    if ($completionStatus -in @(
        'COMPLETION_STATUS_UNSPECIFIED', 'EXECUTION_RUNNING', 'EXECUTION_PENDING'
    )) {
        return 'Unknown'
    }
    if (-not [string]::IsNullOrWhiteSpace($completionStatus)) {
        throw 'Cloud Run Job execution returned an invalid completionStatus'
    }
    $statusObject = Get-ObjectProperty -Object $Execution -Name 'status'
    $conditionsValue = Get-ObjectProperty -Object $statusObject -Name 'conditions'
    if ($null -eq $conditionsValue) {
        $conditionsValue = Get-ObjectProperty -Object $Execution -Name 'conditions'
    }
    $conditions = @($conditionsValue)
    $completed = @($conditions | Where-Object {
        (Get-ObjectProperty -Object $_ -Name 'type') -eq 'Completed'
    })
    if ($completed.Count -gt 1) {
        throw 'Cloud Run Job execution returned duplicate Completed conditions'
    }
    if ($completed.Count -eq 0) {
        return 'Unknown'
    }
    $status = [string](Get-ObjectProperty -Object $completed[0] -Name 'status')
    if ([string]::IsNullOrWhiteSpace($status)) {
        $state = [string](Get-ObjectProperty -Object $completed[0] -Name 'state')
        $status = switch ($state) {
            'CONDITION_SUCCEEDED' { 'True' }
            'CONDITION_FAILED' { 'False' }
            'CONDITION_PENDING' { 'Unknown' }
            'CONDITION_RECONCILING' { 'Unknown' }
            'STATE_UNSPECIFIED' { 'Unknown' }
            default { throw 'Cloud Run Job execution returned an invalid Completed state' }
        }
    }
    if ($status -notin @('True', 'False', 'Unknown')) {
        throw 'Cloud Run Job execution returned an invalid Completed status'
    }
    return $status
}

function Assert-NoActiveJobExecution {
    param([Parameter(Mandatory = $true)][string]$JobName)
    $executions = @(Invoke-GcloudJson -Arguments @(
        'run', 'jobs', 'executions', 'list',
        "--job=$JobName",
        "--region=$Region",
        "--project=$ProjectId"
    ) -TimeoutSeconds 300)
    foreach ($candidate in $executions) {
        $candidateId = Get-ControlledExecutionId -Execution $candidate -JobName $JobName
        if ((Get-ExecutionCompletedStatus -Execution $candidate) -eq 'Unknown') {
            throw "Cloud Run Job already has active execution $candidateId"
        }
    }
}

function Wait-JobExecution {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutionId,
        [Parameter(Mandatory = $true)][string]$JobName
    )
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $execution = Invoke-GcloudJson -Arguments @(
            'run', 'jobs', 'executions', 'describe', $ExecutionId,
            "--region=$Region",
            "--project=$ProjectId"
        ) -TimeoutSeconds 300
        $observedId = Get-ControlledExecutionId -Execution $execution -JobName $JobName
        if ($observedId -cne $ExecutionId) {
            throw "Cloud Run Job execution identity drifted from $ExecutionId"
        }
        $completedStatus = Get-ExecutionCompletedStatus -Execution $execution
        if ($completedStatus -eq 'True') {
            return $ExecutionId
        }
        if ($completedStatus -eq 'False') {
            throw "Cloud Run Job execution $ExecutionId completed unsuccessfully"
        }
        if ($stopwatch.Elapsed.TotalSeconds -ge $jobDeadlineSeconds) {
            throw "Cloud Run Job execution $ExecutionId exceeded the controlled polling deadline"
        }
        Start-Sleep -Milliseconds $jobPollMilliseconds
    }
}

function Assert-ExactBundleContract {
    param([object]$Verification)
    if ($Verification -isnot [pscustomobject]) {
        throw 'verified RAG bundle authority is not one JSON object'
    }
    Assert-ExactObjectProperties -Object $Verification -Names @(
        'access_mode',
        'approved_poster_object_count',
        'bundle_sha256',
        'derived_inventory_sha256',
        'derived_poster_bytes',
        'document_count',
        'document_embedding_profile',
        'documents',
        'embedding_dimension',
        'embedding_model',
        'expected_embedding_count',
        'facet_count',
        'gcs_prefix',
        'generation_model',
        'manifest_sha256',
        'metadata_passage_count',
        'movie_count',
        'parent_release_manifest_sha256',
        'pdf_passage_count',
        'pilot_count',
        'poster_authority_sha256',
        'poster_row_count',
        'primary_poster_row_count',
        'rag_release_id',
        'rag_schema_version',
        'relevance_policy_sha256',
        'schema_version',
        'text_extraction_profile',
        'tier_a_count',
        'tier_b_count',
        'tier_s_count',
        'unavailable_poster_row_count',
        'valid'
    ) -Context 'verified RAG bundle authority'
    $hashNames = @(
        'bundle_sha256',
        'derived_inventory_sha256',
        'manifest_sha256',
        'parent_release_manifest_sha256',
        'poster_authority_sha256',
        'relevance_policy_sha256'
    )
    foreach ($hashName in $hashNames) {
        $hashValue = Get-ObjectProperty -Object $Verification -Name $hashName
        if ($hashValue -isnot [string] -or $hashValue -cnotmatch '^[0-9a-f]{64}$') {
            throw "verified RAG bundle $hashName is not one lowercase SHA-256"
        }
    }
    $releaseId = Get-ObjectProperty -Object $Verification -Name 'rag_release_id'
    $prefix = Get-ObjectProperty -Object $Verification -Name 'gcs_prefix'
    if (
        $releaseId -isnot [string] -or
        $releaseId -cnotmatch '^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$' -or
        $releaseId.Contains('..') -or
        -not (Test-ExactString -Actual $prefix -Expected "rag/$releaseId/")
    ) {
        throw 'verified RAG release ID or canonical GCS prefix is unsafe'
    }
    if (
        (Get-ObjectProperty -Object $Verification -Name 'valid') -isnot [bool] -or
        $Verification.valid -ne $true -or
        $Verification.schema_version -cne 'rag-bundle-verification/v2' -or
        $Verification.rag_schema_version -cne $deploymentContract.rag_schema_version -or
        $Verification.rag_release_id -cne $deploymentContract.release_id -or
        $Verification.parent_release_manifest_sha256 -cne
            $deploymentContract.parent_release_manifest_sha256 -or
        $Verification.movie_count -isnot [long] -or
        $Verification.movie_count -ne $deploymentContract.movie_count -or
        $Verification.facet_count -isnot [long] -or
        $Verification.facet_count -ne $deploymentContract.facet_count -or
        $Verification.tier_s_count -isnot [long] -or
        $Verification.tier_s_count -ne $deploymentContract.tier_s_count -or
        $Verification.tier_a_count -isnot [long] -or
        $Verification.tier_a_count -ne $deploymentContract.tier_a_count -or
        $Verification.tier_b_count -isnot [long] -or
        $Verification.tier_b_count -ne $deploymentContract.tier_b_count -or
        $Verification.pilot_count -isnot [long] -or
        $Verification.pilot_count -ne $deploymentContract.pilot_count -or
        $Verification.poster_row_count -isnot [long] -or
        $Verification.poster_row_count -ne $deploymentContract.movie_count -or
        $Verification.primary_poster_row_count -isnot [long] -or
        $Verification.primary_poster_row_count -ne $deploymentContract.movie_count -or
        $Verification.approved_poster_object_count -isnot [long] -or
        $Verification.approved_poster_object_count -ne $deploymentContract.poster_count -or
        $Verification.unavailable_poster_row_count -isnot [long] -or
        $Verification.unavailable_poster_row_count -ne $deploymentContract.unavailable_poster_count -or
        $Verification.derived_poster_bytes -isnot [long] -or
        $Verification.derived_poster_bytes -lt 1 -or
        $Verification.metadata_passage_count -isnot [long] -or
        $Verification.metadata_passage_count -ne $deploymentContract.movie_count -or
        $Verification.document_count -isnot [long] -or
        $Verification.document_count -ne $deploymentContract.document_count -or
        $Verification.pdf_passage_count -isnot [long] -or
        $Verification.pdf_passage_count -ne $deploymentContract.pdf_passage_count -or
        $Verification.expected_embedding_count -isnot [long] -or
        $Verification.expected_embedding_count -ne $deploymentContract.embedding_count -or
        $Verification.embedding_dimension -isnot [long] -or
        $Verification.embedding_dimension -ne 768 -or
        $Verification.embedding_model -cne 'gemini-embedding-2' -or
        $Verification.generation_model -cne 'gemini-3.5-flash-lite' -or
        $Verification.text_extraction_profile -cne 'cjk-layout-v1' -or
        $Verification.document_embedding_profile -cne 'vertex-title-text-v1' -or
        $Verification.relevance_policy_sha256 -cne
            $deploymentContract.relevance_policy_sha256 -or
        $Verification.poster_authority_sha256 -cne
            $deploymentContract.poster_authority_sha256 -or
        $Verification.access_mode -cne 'restricted_demo'
    ) {
        throw 'verified RAG bundle does not match the selected exact deployment contract'
    }
    if (
        $Verification.documents -isnot [System.Array] -or
        $Verification.documents.Count -ne $deploymentContract.document_count
    ) {
        throw 'verified RAG bundle does not contain the selected exact document bindings'
    }
    $expectedDocuments = @(
        [ordered]@{
            document_id = 'drunken-master-deep-analysis-v1'
            movie_id = '1978_ZQ_001'
            page_count = 3
            quality_status = 'manual_approved'
            rights_status = 'restricted'
            source_filename = 'S 級港⽚-醉拳.pdf'
            source_sha256 = 'ab27baf8e22ceb63a476b65dbfb02951a63a9e9bf39d96a47cd8e3f9fd314a75'
        },
        [ordered]@{
            document_id = 'aces-go-places-deep-analysis-v1'
            movie_id = '1982_ZJPD_001'
            page_count = 3
            quality_status = 'manual_approved'
            rights_status = 'restricted'
            source_filename = 'S 級港⽚-最佳拍檔.pdf'
            source_sha256 = '69fa1ad28916c043689ef4fe826cde816c05a3b254e5a0372d1a57ca2ce63bce'
        },
        [ordered]@{
            document_id = 'its-a-mad-mad-mad-world-deep-analysis-v1'
            movie_id = '1987_FGBR_001'
            page_count = 6
            quality_status = 'manual_approved'
            rights_status = 'restricted'
            source_filename = 'S級港片-富貴逼人.pdf'
            source_sha256 = '5aef7f8684bc0d91d77ffb6d7f89cb323358e2804ef8eaacbc0a02cfeb3d3df4'
        }
    )
    if ($ExpectedRagReleaseId -ceq 'v1.2-demo-r3') {
        $expectedDocuments = @(
            $expectedDocuments[0],
            $expectedDocuments[1],
            [ordered]@{
                document_id = 'mr-vampire-deep-analysis-v1'
                movie_id = '1985_JSXS_001'
                page_count = 4
                quality_status = 'manual_approved'
                rights_status = 'restricted'
                source_filename = 'S 級港⽚-殭屍先生.pdf'
                source_sha256 = '2dce5ec8a1459999c53e9a56f603225e3970061cc7d2f7a35b8d53388096e576'
            },
            $expectedDocuments[2],
            [ordered]@{
                document_id = 'shaolin-soccer-deep-analysis-v1'
                movie_id = '2001_SLZQ_001'
                page_count = 5
                quality_status = 'manual_approved'
                rights_status = 'restricted'
                source_filename = 'S 級港⽚-少林足球.pdf'
                source_sha256 = 'c6f713e2f5c60381d7b8745829f89680ebcfcb631c24d86814a9c6dfcd49e863'
            }
        )
    }
    $observedDocumentsJson = ConvertTo-Json -InputObject @($Verification.documents) -Depth 5 -Compress
    $expectedDocumentsJson = ConvertTo-Json -InputObject $expectedDocuments -Depth 5 -Compress
    if ($observedDocumentsJson -cne $expectedDocumentsJson) {
        throw 'verified RAG bundle document bindings differ from the selected approved sources'
    }
}

function Assert-ExactPosterAllowlist {
    param([string]$RagBundlePath)
    $approved = [System.Collections.Generic.Dictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    $primaryCount = 0
    $posterCount = 0
    Get-Content -LiteralPath $RagBundlePath -Encoding utf8 | ForEach-Object {
        $record = $_ | ConvertFrom-Json
        if ($record.record_kind -ne 'poster') {
            return
        }
        $posterCount += 1
        if ($record.is_primary -ne $true) {
            throw 'RAG bundle contains a non-primary poster-state row'
        }
        $primaryCount += 1
        if ($record.quality_status -in @('machine_passed', 'manual_approved')) {
            $expectedKey = "$posterPrefix$($record.movie_id).webp"
            if (
                $record.derived_object_uri -ne $expectedKey -or
                $record.derived_mime_type -ne 'image/webp' -or
                -not ($record.derived_content_sha256 -match '^[0-9a-f]{64}$') -or
                $record.derived_byte_length -isnot [long] -or
                [int64]$record.derived_byte_length -lt 1 -or
                [int64]$record.derived_byte_length -gt 16777216
            ) {
                throw 'approved poster identity is outside the canonical allowlist'
            }
            $approved.Add([string]$record.movie_id, $record)
        }
    }
    if (
        $posterCount -ne $deploymentContract.movie_count -or
        $primaryCount -ne $deploymentContract.movie_count -or
        $approved.Count -ne $deploymentContract.poster_count
    ) {
        throw 'poster allowlist counts do not match the release contract'
    }

    $derivedRoot = Join-Path $resolvedSourceRoot 'assets/posters/derived'
    $resolvedDerivedRoot = (Resolve-Path -LiteralPath $derivedRoot).Path
    $files = @(Get-ChildItem -LiteralPath $resolvedDerivedRoot -Force)
    if (
        $files.Count -ne $deploymentContract.poster_count -or
        $files.Where({ -not $_.PSIsContainer }).Count -ne $deploymentContract.poster_count
    ) {
        throw 'derived poster directory is not the selected exact file allowlist'
    }
    foreach ($file in $files) {
        if (
            $file.PSIsContainer -or
            $file.LinkType -or
            $file.Extension -cne '.webp' -or
            -not $approved.ContainsKey($file.BaseName)
        ) {
            throw 'derived poster directory contains a non-allowlisted entry'
        }
        $record = $approved[$file.BaseName]
        $stream = [System.IO.File]::OpenRead($file.FullName)
        try {
            $actualSha256 = [Convert]::ToHexString(
                [System.Security.Cryptography.SHA256]::HashData($stream)
            ).ToLowerInvariant()
        }
        finally {
            $stream.Dispose()
        }
        if (
            $file.Length -lt 1 -or
            $file.Length -gt 16777216 -or
            [int64]$record.derived_byte_length -ne $file.Length -or
            $actualSha256 -cne $record.derived_content_sha256
        ) {
            throw 'derived poster bytes changed after bundle construction'
        }
    }
    return $resolvedDerivedRoot
}

$trackedStatus = Invoke-Local -Executable 'git' -Arguments @(
    'status', '--porcelain', '--untracked-files=all'
) -WorkingDirectory $repoRoot
if (-not [string]::IsNullOrWhiteSpace($trackedStatus)) {
    throw 'deployment requires a clean worktree so the image tag identifies its source'
}
$gitCommit = Get-ExactGitCommit
$gitSha = $gitCommit.Substring(0, 12)
if ($script:CloudRunV2UsesTestCredential) {
    $candidateTag = 'accept-000000000000'
    $candidateRevisionSuffix = '00006-new'
}
else {
    $candidateNonceBytes = [byte[]]::new(8)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($candidateNonceBytes)
    $candidateNonce = [System.Convert]::ToHexString(
        $candidateNonceBytes
    ).ToLowerInvariant()
    # Cloud Run requires the service name and traffic tag to total at most 46
    # characters. Keep the immutable commit prefix plus a full 64-bit nonce.
    $candidateTag = "a-$($gitSha.Substring(0, 8))-$candidateNonce"
    $candidateRevisionSuffix = "00000-$($deploymentContract.candidate_suffix)-$gitSha-$candidateNonce"
}
$expectedCandidateRevision = "$service-$candidateRevisionSuffix"
if (
    $candidateTag.Length -gt 63 -or
    $service.Length + $candidateTag.Length -gt 46 -or
    $candidateTag -cnotmatch '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])$' -or
    $expectedCandidateRevision.Length -gt 63 -or
    $expectedCandidateRevision -cnotmatch (
        '^' + [regex]::Escape($service) + '-[0-9]{5}-[a-z0-9][a-z0-9-]*$'
    )
) {
    throw 'generated Cloud Run candidate tag or exact revision name is unsafe'
}

$null = Invoke-Local -Executable 'uv' -Arguments @(
    'run', 'hk-movie-rag', 'verify-release',
    (Join-Path $resolvedSourceRoot 'data/release/v1.2/release_manifest.json')
) -WorkingDirectory $repoRoot

$null = Invoke-Local -Executable 'uv' -Arguments @(
    'run', 'hk-movie-rag', 'build-rag-bundle',
    '--source-root', $resolvedSourceRoot,
    '--document-source-root', $resolvedDocumentSourceRoot,
    '--config', $resolvedRagConfigPath,
    '--output', $BundleDirectory
) -WorkingDirectory $repoRoot

$manifestPath = Join-Path $BundleDirectory 'rag_release_manifest.json'
$ragBundlePath = Join-Path $BundleDirectory 'rag_bundle.jsonl'
$verificationText = Invoke-Local -Executable 'uv' -Arguments @(
    'run', 'hk-movie-rag', 'verify-rag-bundle', $manifestPath
) -WorkingDirectory $repoRoot
$verification = ConvertFrom-ExactJsonObject `
    -Text $verificationText `
    -Context 'verified RAG bundle authority'
Assert-ExactBundleContract -Verification $verification
$releaseId = [string]$verification.rag_release_id
$bundlePrefix = [string]$verification.gcs_prefix
$manifestObject = "${bundlePrefix}rag_release_manifest.json"
$bundleObject = "${bundlePrefix}rag_bundle.jsonl"
$manifestSha = [string]$verification.manifest_sha256
$bundleSha = [string]$verification.bundle_sha256
if ($ReuseFromReleaseId -ceq $releaseId) {
    throw 'reuse source and target release IDs must differ'
}
if (
    (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
        $manifestSha -or
    (Get-FileHash -LiteralPath $ragBundlePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
        $bundleSha
) {
    throw 'verified RAG bundle hashes differ from the exact local artifacts'
}
$derivedRoot = Assert-ExactPosterAllowlist -RagBundlePath $ragBundlePath
Assert-GitSourcePinned -ExpectedCommit $gitCommit

# Capture a restorable, non-floating production state before any cloud mutation.
$priorServiceDescription = Invoke-GcloudObject -Arguments @(
    'run', 'services', 'describe', $service,
    "--region=$Region",
    "--project=$ProjectId"
)
$priorService = Assert-ServiceObservation -Description $priorServiceDescription
$priorServiceUrl = $priorService.Url
$priorTraffic = Get-CloudRunV1TrafficSnapshot `
    -Status $priorService.Status `
    -ExpectedCandidateTag $candidateTag
$priorRevision = $priorTraffic.StableRevision
$priorLatestCreatedRevision = Get-ServiceRevisionName `
    -Status $priorService.Status `
    -Name 'latestCreatedRevisionName'
$priorLatestReadyRevision = Get-ServiceRevisionName `
    -Status $priorService.Status `
    -Name 'latestReadyRevisionName'
if (
    $priorLatestReadyRevision -cne $priorRevision -and
    $priorLatestReadyRevision -cne $priorLatestCreatedRevision
) {
    throw 'Cloud Run prior ready revision is outside the captured serving/candidate set'
}
$capturedReusableCandidateV2 = $null
if (-not [string]::IsNullOrEmpty($priorTraffic.CandidateRevision)) {
    if ($priorTraffic.CandidateRevision -cne $priorLatestCreatedRevision) {
        throw 'Cloud Run prior candidate tag does not match the latest created revision'
    }
    # A rerun may encounter the same verified no-traffic candidate. Capture its
    # v2 etag before any mutation so reuse cannot hide a concurrent owner.
    $capturedReusableCandidateV2 = Get-CloudRunV2TaggedState `
        -StableRevision $priorRevision `
        -CandidateRevision $priorTraffic.CandidateRevision `
        -CandidateTag $candidateTag `
        -CandidatePercent 0 `
        -CandidateUrl $priorTraffic.CandidateUrl `
        -ExpectedLatestCreatedRevision $priorLatestCreatedRevision `
        -ExpectedLatestReadyRevisions @(
            $priorRevision,
            $priorLatestCreatedRevision
        ) `
        -ExpectedUrl $priorServiceUrl
}
else {
    $null = Get-CloudRunV2ServiceState `
        -ExpectedTrafficRevision $priorRevision `
        -ExpectedLatestCreatedRevision $priorLatestCreatedRevision `
        -ExpectedLatestReadyRevisions @(
            $priorRevision,
            $priorLatestCreatedRevision
        ) `
        -ExpectedUrl $priorServiceUrl
}
$dbSecretVersion = Get-EnabledSecretVersion -Name $dbSecret

# Fail closed if provisioning drifted before any release upload.
$bucketDescription = Invoke-GcloudJson -Arguments @(
    'storage', 'buckets', 'describe', "gs://$bucket", "--project=$ProjectId"
)
if (
    $bucketDescription.location -ne 'US-CENTRAL1' -or
    $bucketDescription.uniform_bucket_level_access -ne $true -or
    $bucketDescription.public_access_prevention -ne 'enforced'
) {
    throw 'release bucket is not the expected private, uniform-access bucket'
}

$legacyManifestUri = "gs://$bucket/$($deploymentContract.reuse_manifest_prefix)/rag_release_manifest.json"
$legacyBundleUri = "gs://$bucket/$($deploymentContract.reuse_manifest_prefix)/rag_bundle.jsonl"
$legacyManifestDescription = Get-GcsObjectDescription -Uri $legacyManifestUri
if (
    $null -eq $legacyManifestDescription -or
    (Get-ImmutableReleaseObjectSha256 -Description $legacyManifestDescription) -cne
        $deploymentContract.reuse_manifest_sha256 -or
    [string]$legacyManifestDescription.generation -cne $deploymentContract.reuse_manifest_generation -or
    [int64]$legacyManifestDescription.size -lt 1 -or
    $legacyManifestDescription.content_type -cne 'application/json' -or
    $legacyManifestDescription.cache_control -cne 'no-store'
) {
    throw 'legacy source manifest is not the exact immutable reuse authority'
}
$legacyBundleDescription = Get-GcsObjectDescription -Uri $legacyBundleUri
if (
    $null -eq $legacyBundleDescription -or
    (Get-ImmutableReleaseObjectSha256 -Description $legacyBundleDescription) -cne
        $deploymentContract.reuse_bundle_sha256 -or
    [string]$legacyBundleDescription.generation -cne $deploymentContract.reuse_bundle_generation -or
    [int64]$legacyBundleDescription.size -ne $deploymentContract.reuse_bundle_size -or
    $legacyBundleDescription.content_type -cne 'application/x-ndjson' -or
    $legacyBundleDescription.cache_control -cne 'no-store'
) {
    throw 'legacy source bundle is not the exact immutable reuse authority'
}

Assert-ImmutableReleaseObject `
    -LocalPath $manifestPath `
    -Uri "gs://$bucket/$manifestObject" `
    -ContentType 'application/json' `
    -Sha256 $manifestSha
Assert-ImmutableReleaseObject `
    -LocalPath $ragBundlePath `
    -Uri "gs://$bucket/$bundleObject" `
    -ContentType 'application/x-ndjson' `
    -Sha256 $bundleSha
$null = Invoke-Gcloud -Arguments @(
    'storage', 'rsync', $derivedRoot, "gs://$bucket/$posterPrefix",
    '--recursive',
    '--checksums-only',
    '--no-clobber',
    '--if-generation-match=0',
    '--content-type=image/webp',
    '--cache-control=private,no-store',
    "--project=$ProjectId",
    '--quiet'
)

$remotePosterText = Invoke-Gcloud -Arguments @(
    'storage', 'ls', "gs://$bucket/$posterPrefix**",
    "--project=$ProjectId"
)
$remotePosterObjects = @(
    $remotePosterText -split "`r?`n" |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { $_.Trim() }
)
if ($remotePosterObjects.Count -ne $deploymentContract.poster_count) {
    throw 'remote poster prefix does not contain the selected exact object count'
}
$expectedPosterObjects = @(
    Get-ChildItem -LiteralPath $derivedRoot -File |
        ForEach-Object { "gs://$bucket/$posterPrefix$($_.Name)" } |
        Sort-Object
)
if (Compare-Object -ReferenceObject $expectedPosterObjects -DifferenceObject @(
    $remotePosterObjects | Sort-Object
)) {
    throw 'remote poster object identities differ from the canonical local allowlist'
}

$imageRepository = "$Region-docker.pkg.dev/$ProjectId/$repository/demo"
$image = "$imageRepository`:$gitSha"
$temporaryBuildRoot = New-TemporaryBuildRoot
try {
    $archivePath = Join-Path $temporaryBuildRoot 'committed-source.zip'
    $buildContext = Join-Path $temporaryBuildRoot 'context'
    $null = Invoke-Local -Executable 'git' -Arguments @(
        'archive', '--format=zip', "--output=$archivePath", $gitCommit
    ) -WorkingDirectory $repoRoot -TimeoutSeconds 300
    $null = New-Item -ItemType Directory -Path $buildContext
    [System.IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $buildContext)
    Assert-CommittedBuildContext -ContextPath $buildContext
    $null = Invoke-Gcloud -Arguments @(
        'builds', 'submit', $buildContext,
        '--tag', $image,
        "--project=$ProjectId",
        '--quiet'
    )
}
finally {
    Remove-TemporaryBuildRoot -Path $temporaryBuildRoot
}
$imageDescription = Invoke-GcloudJson -Arguments @(
    'artifacts', 'docker', 'images', 'describe', $image,
    "--project=$ProjectId"
)
$imageDigest = $imageDescription.image_summary.digest
if ([string]::IsNullOrWhiteSpace($imageDigest)) {
    $imageDigest = $imageDescription.imageSummary.digest
}
if ($imageDigest -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Artifact Registry did not return an immutable image digest'
}
$immutableImage = "$imageRepository@$imageDigest"

$instanceDescription = Invoke-GcloudJson -Arguments @(
    'sql', 'instances', 'describe', $instance, "--project=$ProjectId"
)
$instanceConnection = $instanceDescription.connectionName
if ($instanceConnection -ne "$ProjectId`:$Region`:$instance") {
    throw 'Cloud SQL connection identity does not match the exact deployment contract'
}

$jobEnvironment = @(
    "DB_NAME=$database",
    "DB_USER=$databaseUser",
    "INSTANCE_CONNECTION_NAME=$instanceConnection",
    "RAG_GCP_PROJECT_ID=$ProjectId",
    'RAG_VERTEX_LOCATION=global'
) -join ','
$manifestUri = "gs://$bucket/$manifestObject"
$legacyClaimArguments = (
    "ingest-rag-bundle,$legacyManifestUri," +
    "--require-existing-active,--require-embedded,0," +
    "--require-skipped,$($deploymentContract.reuse_release_embedding_count)"
)
$initialIngestArguments = (
    "ingest-rag-bundle,$manifestUri," +
    "--reuse-from-release-id,$ReuseFromReleaseId," +
    "--require-source-eligible-total,$($deploymentContract.reusable_count)," +
    "--require-non-reusable-embedded-total,$($deploymentContract.new_embedding_count)"
)
$activeRerunArguments = (
    "$initialIngestArguments," +
    '--require-existing-active,' +
    "--require-embedded,0,--require-skipped,$($deploymentContract.embedding_count)"
)
$ingestJobDeploymentArguments = @(
    'run', 'jobs', 'deploy', $job,
    '--image', $immutableImage,
    "--region=$Region",
    "--service-account=$serviceAccount",
    '--set-cloudsql-instances', $instanceConnection,
    '--set-env-vars', $jobEnvironment,
    '--set-secrets', "DB_PASSWORD=$dbSecret`:$dbSecretVersion",
    '--command=hk-movie-rag',
    '--tasks=1',
    '--max-retries=0',
    '--task-timeout=86400s',
    '--cpu=2',
    '--memory=2Gi',
    "--project=$ProjectId",
    '--quiet'
)
Assert-NoActiveJobExecution -JobName $job
$null = Invoke-Gcloud -Arguments ($ingestJobDeploymentArguments + @(
    '--args', $legacyClaimArguments
))
Assert-NoActiveJobExecution -JobName $job
$legacyClaimExecution = Invoke-GcloudJson -Arguments @(
    'run', 'jobs', 'execute', $job,
    "--region=$Region",
    "--project=$ProjectId",
    '--quiet'
)
$legacyClaimExecutionId = Get-ControlledExecutionId `
    -Execution $legacyClaimExecution `
    -JobName $job
$null = Wait-JobExecution `
    -ExecutionId $legacyClaimExecutionId `
    -JobName $job

Assert-NoActiveJobExecution -JobName $job
$null = Invoke-Gcloud -Arguments ($ingestJobDeploymentArguments + @(
    '--args', $initialIngestArguments
))
Assert-NoActiveJobExecution -JobName $job
$execution = Invoke-GcloudJson -Arguments @(
    'run', 'jobs', 'execute', $job,
    "--region=$Region",
    "--project=$ProjectId",
    '--quiet'
)
$executionId = Get-ControlledExecutionId -Execution $execution -JobName $job
$null = Wait-JobExecution -ExecutionId $executionId -JobName $job

Assert-NoActiveJobExecution -JobName $job
$null = Invoke-Gcloud -Arguments ($ingestJobDeploymentArguments + @(
    '--args', $activeRerunArguments
))
Assert-NoActiveJobExecution -JobName $job
$activeRerunExecution = Invoke-GcloudJson -Arguments @(
    'run', 'jobs', 'execute', $job,
    "--region=$Region",
    "--project=$ProjectId",
    '--quiet'
)
$activeRerunExecutionId = Get-ControlledExecutionId `
    -Execution $activeRerunExecution `
    -JobName $job
$null = Wait-JobExecution `
    -ExecutionId $activeRerunExecutionId `
    -JobName $job

$posterVerifyEnvironment = @(
    "DB_NAME=$database",
    "DB_USER=$databaseUser",
    "INSTANCE_CONNECTION_NAME=$instanceConnection",
    "GOOGLE_CLOUD_PROJECT=$ProjectId"
) -join ','
$null = Invoke-Gcloud -Arguments @(
    'run', 'jobs', 'deploy', $posterVerifyJob,
    '--image', $immutableImage,
    "--region=$Region",
    "--service-account=$serviceAccount",
    '--set-cloudsql-instances', $instanceConnection,
    '--set-env-vars', $posterVerifyEnvironment,
    '--set-secrets', "DB_PASSWORD=$dbSecret`:$dbSecretVersion",
    '--command=hk-movie-rag',
    '--args', "verify-poster-fleet,--manifest,$manifestUri,--bucket,$bucket,--workers,16",
    '--tasks=1',
    '--max-retries=0',
    '--task-timeout=86400s',
    '--cpu=2',
    '--memory=2Gi',
    "--project=$ProjectId",
    '--quiet'
)
Assert-NoActiveJobExecution -JobName $posterVerifyJob
$posterVerifyExecution = Invoke-GcloudJson -Arguments @(
    'run', 'jobs', 'execute', $posterVerifyJob,
    "--region=$Region",
    "--project=$ProjectId",
    '--quiet'
)
$posterVerifyExecutionId = Get-ControlledExecutionId `
    -Execution $posterVerifyExecution `
    -JobName $posterVerifyJob
$null = Wait-JobExecution `
    -ExecutionId $posterVerifyExecutionId `
    -JobName $posterVerifyJob

$serviceEnvironment = @(
    "GOOGLE_CLOUD_PROJECT=$ProjectId",
    'VERTEX_LOCATION=global',
    "RAG_RELEASE_ID=$releaseId",
    "RAG_RELEASE_MANIFEST_SHA256=$manifestSha",
    "RAG_EMBEDDING_MODEL=$($verification.embedding_model)",
    "RAG_EMBEDDING_DIMENSION=$($verification.embedding_dimension)",
    "RAG_GENERATION_MODEL=$($verification.generation_model)",
    "RAG_RELEASE_BUCKET=$bucket",
    "RAG_RELEVANCE_POLICY_SHA256=$($verification.relevance_policy_sha256)",
    "RAG_POSTER_AUTHORITY_SHA256=$($verification.poster_authority_sha256)",
    "RAG_IMAGE_DIGEST=$imageDigest",
    "DB_NAME=$database",
    "DB_USER=$databaseUser",
    "INSTANCE_CONNECTION_NAME=$instanceConnection"
) -join ','
$candidateSmokePassed = $false
$stableSmokePassed = $false
$tagCleanupPassed = $false
$trafficPhase = 'candidate_unowned'
$ownedEtag = ''
$candidateRevision = ''
$candidateTagUrl = ''
$candidateTagOwnedByThisApply = $null -eq $capturedReusableCandidateV2
$acceptanceStage = 'service_deploy_and_ownership'
try {
    # Capture exact v2 ownership immediately after the mutation. Every later
    # validation failure can then remove only this owned tag with an etag CAS.
    $serviceDeploymentResult = Invoke-GcloudResult -Arguments @(
        'run', 'deploy', $service,
        '--image', $immutableImage,
        '--revision-suffix', $candidateRevisionSuffix,
        "--region=$Region",
        "--service-account=$serviceAccount",
        '--set-cloudsql-instances', $instanceConnection,
        '--set-env-vars', $serviceEnvironment,
        '--set-secrets', "DB_PASSWORD=$dbSecret`:$dbSecretVersion",
        '--no-invoker-iam-check',
        '--port=8080',
        '--cpu=1',
        '--memory=1Gi',
        '--concurrency=20',
        '--min-instances=0',
        '--max-instances=3',
        '--timeout=300s',
        '--no-traffic',
        '--tag', $candidateTag,
        "--project=$ProjectId",
        '--format=json',
        '--quiet'
    ) -AllowFailure `
        -CaptureTimeoutFailure `
        -TimeoutSeconds $serviceDeployTimeoutSeconds
    $candidateOwnership = Wait-CloudRunV2NewCandidateOwnership `
        -StableRevision $priorRevision `
        -PriorLatestCreatedRevision $priorLatestCreatedRevision `
        -ExpectedCandidateRevision $expectedCandidateRevision `
        -CandidateTag $candidateTag `
        -ExpectedUrl $priorServiceUrl
    $candidateRevision = $candidateOwnership.CandidateRevision
    $candidateTagUrl = $candidateOwnership.CandidateUrl
    $ownedEtag = $candidateOwnership.Etag
    $trafficPhase = 'candidate_tagged'

    if ($serviceDeploymentResult.ExitCode -ne 0) {
        throw 'Cloud Run deploy failed after exact candidate ownership was captured'
    }
    $serviceDeploymentText = $serviceDeploymentResult.Output

    $acceptanceStage = 'service_deployment_validation'
    $reusingCapturedCandidate = $candidateRevision -ceq $priorLatestCreatedRevision
    if ($reusingCapturedCandidate) {
        if (
            $null -eq $capturedReusableCandidateV2 -or
            -not (Test-ExactString `
                -Actual $ownedEtag `
                -Expected $capturedReusableCandidateV2.Etag)
        ) {
            throw 'Cloud Run reusable candidate changed outside the captured v2 ownership snapshot'
        }
    }

    $serviceDeployment = ConvertFrom-ExactJsonObject `
        -Text $serviceDeploymentText `
        -Context 'Cloud Run deployment result'
    $deploymentObservation = Assert-ServiceObservation `
        -Description $serviceDeployment `
        -ExpectedUrl $priorServiceUrl
    $deployedRevision = Get-ServiceRevisionName `
        -Status $deploymentObservation.Status `
        -Name 'latestCreatedRevisionName'
    if ($deployedRevision -cne $candidateRevision) {
        throw 'Cloud Run deploy result does not identify the owned no-traffic candidate revision'
    }

    $acceptanceStage = 'candidate_service_validation'
    $candidateServiceDescription = Invoke-GcloudObject -Arguments @(
        'run', 'services', 'describe', $service,
        "--region=$Region",
        "--project=$ProjectId"
    )
    $candidateService = Assert-ServiceObservation `
        -Description $candidateServiceDescription `
        -ExpectedUrl $priorServiceUrl
    $latestCreatedRevision = Get-ServiceRevisionName `
        -Status $candidateService.Status `
        -Name 'latestCreatedRevisionName'
    $latestReadyRevision = Get-ServiceRevisionName `
        -Status $candidateService.Status `
        -Name 'latestReadyRevisionName'
    if (
        $latestCreatedRevision -cne $candidateRevision -or
        (
            $latestReadyRevision -cne $priorRevision -and
            $latestReadyRevision -cne $candidateRevision
        )
    ) {
        throw 'Cloud Run candidate service contains an unexpected created or ready revision'
    }
    $candidateTraffic = Get-CloudRunV1TrafficSnapshot `
        -Status $candidateService.Status `
        -ExpectedCandidateTag $candidateTag
    if (
        $candidateTraffic.StableRevision -cne $priorRevision -or
        $candidateTraffic.CandidateRevision -cne $candidateRevision -or
        $candidateTraffic.CandidateUrl -cne $candidateTagUrl
    ) {
        throw 'Cloud Run tagged candidate changed prior traffic or its owned URL'
    }

    $acceptanceStage = 'candidate_revision_validation'
    $candidateRevisionDescription = Invoke-GcloudObject -Arguments @(
        'run', 'revisions', 'describe', $candidateRevision,
        "--region=$Region",
        "--project=$ProjectId"
    )
    Assert-CandidateRevision `
        -Description $candidateRevisionDescription `
        -Revision $candidateRevision `
        -ImmutableImage $immutableImage `
        -InstanceConnection $instanceConnection `
        -DbSecretVersion $dbSecretVersion

    $acceptanceStage = 'pre_smoke_secret_validation'
    $preSmokeDbSecretVersion = Get-EnabledSecretVersion -Name $dbSecret
    if ($preSmokeDbSecretVersion -cne $dbSecretVersion) {
        throw 'deployment secret versions changed before candidate smoke'
    }

    $acceptanceStage = 'candidate_smoke'
    $null = Get-CloudRunV2TaggedState `
        -StableRevision $priorRevision `
        -CandidateRevision $candidateRevision `
        -CandidateTag $candidateTag `
        -CandidatePercent 0 `
        -CandidateUrl $candidateTagUrl `
        -ExpectedLatestCreatedRevision $candidateRevision `
        -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
        -ExpectedUrl $priorServiceUrl `
        -ExpectedEtag $ownedEtag

    Invoke-LiveSmoke -BaseUrl $candidateTagUrl `
        -ExpectedRevision $candidateRevision `
        -ExpectedImageDigest $imageDigest `
        -ManifestPath $manifestPath `
        -Phase 'candidate'
    $candidateSmokePassed = $true

    # The smoke may take up to 1,800 seconds. Resolve the DB secret version
    # again after it and immediately before the conditional promotion.
    $acceptanceStage = 'pre_promotion_secret_validation'
    $freshDbSecretVersion = Get-EnabledSecretVersion -Name $dbSecret
    if ($freshDbSecretVersion -cne $dbSecretVersion) {
        throw 'deployment secret versions changed after candidate smoke'
    }

    $acceptanceStage = 'promotion'
    $null = Get-CloudRunV2TaggedState `
        -StableRevision $priorRevision `
        -CandidateRevision $candidateRevision `
        -CandidateTag $candidateTag `
        -CandidatePercent 0 `
        -CandidateUrl $candidateTagUrl `
        -ExpectedLatestCreatedRevision $candidateRevision `
        -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
        -ExpectedUrl $priorServiceUrl `
        -ExpectedEtag $ownedEtag
    $promotionCas = Invoke-CloudRunV2TaggedPromotionCas `
        -ExpectedEtag $ownedEtag `
        -CandidateRevision $candidateRevision `
        -CandidateTag $candidateTag `
        -CandidateUrl $candidateTagUrl `
        -ExpectedUrl $priorServiceUrl
    $ownedEtag = $promotionCas.Etag
    $trafficPhase = 'candidate_promoted_tagged'
    $acceptanceStage = 'stable_smoke'
    $null = Get-CloudRunV2TaggedState `
        -StableRevision '' `
        -CandidateRevision $candidateRevision `
        -CandidateTag $candidateTag `
        -CandidatePercent 100 `
        -CandidateUrl $candidateTagUrl `
        -ExpectedLatestCreatedRevision $candidateRevision `
        -ExpectedLatestReadyRevisions @($candidateRevision) `
        -ExpectedUrl $priorServiceUrl `
        -ExpectedEtag $ownedEtag

    Invoke-LiveSmoke -BaseUrl $priorServiceUrl `
        -ExpectedRevision $candidateRevision `
        -ExpectedImageDigest $imageDigest `
        -ManifestPath $manifestPath `
        -Phase 'stable'
    $stableSmokePassed = $true

    $acceptanceStage = 'candidate_tag_cleanup'
    $preCleanup = Get-CloudRunV2TaggedState `
        -StableRevision '' `
        -CandidateRevision $candidateRevision `
        -CandidateTag $candidateTag `
        -CandidatePercent 100 `
        -CandidateUrl $candidateTagUrl `
        -ExpectedLatestCreatedRevision $candidateRevision `
        -ExpectedLatestReadyRevisions @($candidateRevision) `
        -ExpectedUrl $priorServiceUrl `
        -ExpectedEtag $ownedEtag
    $cleanupCas = Invoke-CloudRunV2TagCleanupCas `
        -ExpectedEtag $preCleanup.Etag `
        -TargetRevision $candidateRevision `
        -CandidateRevision $candidateRevision `
        -ExpectedLatestReadyRevisions @($candidateRevision) `
        -ExpectedUrl $priorServiceUrl
    $ownedEtag = $cleanupCas.Etag
    $tagCleanupPassed = $true
    $trafficPhase = 'candidate_promoted_clean'

    $acceptanceStage = 'post_cleanup_validation'
    $postServiceDescription = Invoke-GcloudObject -Arguments @(
        'run', 'services', 'describe', $service,
        "--region=$Region",
        "--project=$ProjectId"
    )
    $postService = Assert-ServiceObservation `
        -Description $postServiceDescription `
        -ExpectedUrl $priorServiceUrl
    $postCreatedRevision = Get-ServiceRevisionName `
        -Status $postService.Status `
        -Name 'latestCreatedRevisionName'
    $postReadyRevision = Get-ServiceRevisionName `
        -Status $postService.Status `
        -Name 'latestReadyRevisionName'
    $postTrafficRevision = Get-ExactPinnedTrafficRevision -Status $postService.Status
    if (
        $postCreatedRevision -cne $candidateRevision -or
        $postReadyRevision -cne $candidateRevision -or
        $postTrafficRevision -cne $candidateRevision
    ) {
        throw 'Cloud Run post-cleanup state is not the exact accepted candidate revision'
    }
    $postRevisionDescription = Invoke-GcloudObject -Arguments @(
        'run', 'revisions', 'describe', $candidateRevision,
        "--region=$Region",
        "--project=$ProjectId"
    )
    Assert-CandidateRevision `
        -Description $postRevisionDescription `
        -Revision $candidateRevision `
        -ImmutableImage $immutableImage `
        -InstanceConnection $instanceConnection `
        -DbSecretVersion $dbSecretVersion
    $null = Get-CloudRunV2ServiceState `
        -ExpectedTrafficRevision $candidateRevision `
        -ExpectedLatestCreatedRevision $candidateRevision `
        -ExpectedLatestReadyRevisions @($candidateRevision) `
        -ExpectedUrl $priorServiceUrl `
        -ExpectedEtag $ownedEtag
}
catch {
    $rollbackStatus = 'rollback_skipped_unowned'
    if ($trafficPhase -ceq 'candidate_tagged' -and $candidateTagOwnedByThisApply) {
        try {
            $ownedCandidate = Get-CloudRunV2TaggedState `
                -StableRevision $priorRevision `
                -CandidateRevision $candidateRevision `
                -CandidateTag $candidateTag `
                -CandidatePercent 0 `
                -CandidateUrl $candidateTagUrl `
                -ExpectedLatestCreatedRevision $candidateRevision `
                -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                -ExpectedUrl $priorServiceUrl `
                -ExpectedEtag $ownedEtag
            $cleanup = Invoke-CloudRunV2TagCleanupCas `
                -ExpectedEtag $ownedCandidate.Etag `
                -TargetRevision $priorRevision `
                -CandidateRevision $candidateRevision `
                -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                -ExpectedUrl $priorServiceUrl
            $null = Get-CloudRunV2ServiceState `
                -ExpectedTrafficRevision $priorRevision `
                -ExpectedLatestCreatedRevision $candidateRevision `
                -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                -ExpectedUrl $priorServiceUrl `
                -ExpectedEtag $cleanup.Etag
            $rollbackStatus = 'candidate_tag_removed'
        }
        catch {
            # Any state or etag drift belongs to another actor; never overwrite it.
        }
    }
    elseif ($trafficPhase -ceq 'candidate_promoted_tagged') {
        try {
            $ownedPromotion = Get-CloudRunV2TaggedState `
                -StableRevision '' `
                -CandidateRevision $candidateRevision `
                -CandidateTag $candidateTag `
                -CandidatePercent 100 `
                -CandidateUrl $candidateTagUrl `
                -ExpectedLatestCreatedRevision $candidateRevision `
                -ExpectedLatestReadyRevisions @($candidateRevision) `
                -ExpectedUrl $priorServiceUrl `
                -ExpectedEtag $ownedEtag
            $rollbackTagged = Invoke-CloudRunV2TaggedRollbackCas `
                -ExpectedEtag $ownedPromotion.Etag `
                -StableRevision $priorRevision `
                -CandidateRevision $candidateRevision `
                -CandidateTag $candidateTag `
                -CandidateUrl $candidateTagUrl `
                -ExpectedUrl $priorServiceUrl
            if ($candidateTagOwnedByThisApply) {
                $cleanupRollback = Invoke-CloudRunV2TagCleanupCas `
                    -ExpectedEtag $rollbackTagged.Etag `
                    -TargetRevision $priorRevision `
                    -CandidateRevision $candidateRevision `
                    -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                    -ExpectedUrl $priorServiceUrl
                $null = Get-CloudRunV2ServiceState `
                    -ExpectedTrafficRevision $priorRevision `
                    -ExpectedLatestCreatedRevision $candidateRevision `
                    -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                    -ExpectedUrl $priorServiceUrl `
                    -ExpectedEtag $cleanupRollback.Etag
                $rollbackStatus = 'rollback_and_tag_cleanup_verified'
            }
            else {
                $null = Get-CloudRunV2TaggedState `
                    -StableRevision $priorRevision `
                    -CandidateRevision $candidateRevision `
                    -CandidateTag $candidateTag `
                    -CandidatePercent 0 `
                    -CandidateUrl $candidateTagUrl `
                    -ExpectedLatestCreatedRevision $candidateRevision `
                    -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                    -ExpectedUrl $priorServiceUrl `
                    -ExpectedEtag $rollbackTagged.Etag
                $rollbackStatus = 'rollback_with_preexisting_tag_verified'
            }
        }
        catch {
            # Never retry an owned conditional rollback or expose response data.
        }
    }
    elseif ($trafficPhase -ceq 'candidate_promoted_clean') {
        try {
            $ownedClean = Get-CloudRunV2ServiceState `
                -ExpectedTrafficRevision $candidateRevision `
                -ExpectedLatestCreatedRevision $candidateRevision `
                -ExpectedLatestReadyRevisions @($candidateRevision) `
                -ExpectedUrl $priorServiceUrl `
                -ExpectedEtag $ownedEtag
            if ($candidateTagOwnedByThisApply) {
                $rollbackClean = Invoke-CloudRunV2TrafficCas `
                    -ExpectedEtag $ownedClean.Etag `
                    -TargetRevision $priorRevision `
                    -ExpectedLatestCreatedRevision $candidateRevision `
                    -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                    -ExpectedUrl $priorServiceUrl
                $null = Get-CloudRunV2ServiceState `
                    -ExpectedTrafficRevision $priorRevision `
                    -ExpectedLatestCreatedRevision $candidateRevision `
                    -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                    -ExpectedUrl $priorServiceUrl `
                    -ExpectedEtag $rollbackClean.Etag
                $rollbackStatus = 'post_cleanup_rollback_verified'
            }
            else {
                $rollbackClean = Invoke-CloudRunV2TaggedRollbackCas `
                    -ExpectedEtag $ownedClean.Etag `
                    -StableRevision $priorRevision `
                    -CandidateRevision $candidateRevision `
                    -CandidateTag $candidateTag `
                    -CandidateUrl $candidateTagUrl `
                    -ExpectedUrl $priorServiceUrl
                $null = Get-CloudRunV2TaggedState `
                    -StableRevision $priorRevision `
                    -CandidateRevision $candidateRevision `
                    -CandidateTag $candidateTag `
                    -CandidatePercent 0 `
                    -CandidateUrl $candidateTagUrl `
                    -ExpectedLatestCreatedRevision $candidateRevision `
                    -ExpectedLatestReadyRevisions @($priorRevision, $candidateRevision) `
                    -ExpectedUrl $priorServiceUrl `
                    -ExpectedEtag $rollbackClean.Etag
                $rollbackStatus = 'post_cleanup_rollback_with_preexisting_tag_verified'
            }
        }
        catch {
            # Never overwrite state after an ownership drift.
        }
    }
    $redactedReportStatus = if (Test-CurrentLiveSmokeFailureReport) {
        "redacted_report=$($script:LiveSmokeReportRelativePath)"
    }
    else {
        'redacted_report=not_available'
    }
    throw (
        "Cloud Run candidate acceptance failed; failure_stage=$acceptanceStage; " +
        "$rollbackStatus; $redactedReportStatus"
    )
}

$serviceUrl = $priorServiceUrl
$revision = $candidateRevision

[ordered]@{
    schema_version = 'rag-demo-deployment-result/v1'
    project_id = $ProjectId
    region = $Region
    release_id = $releaseId
    service_url = $serviceUrl
    revision = $revision
    image = $immutableImage
    image_digest = $imageDigest
    legacy_claim_execution = $legacyClaimExecutionId
    job_execution = $executionId
    active_rerun_execution = $activeRerunExecutionId
    poster_verify_execution = $posterVerifyExecutionId
    bundle_sha256 = $bundleSha
    manifest_sha256 = $manifestSha
    poster_object_count = $deploymentContract.poster_count
    candidate_smoke_passed = $candidateSmokePassed
    stable_smoke_passed = $stableSmokePassed
    candidate_tag_removed = $tagCleanupPassed
} | ConvertTo-Json -Depth 5 -Compress
