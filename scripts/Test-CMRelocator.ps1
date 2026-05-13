<#
.SYNOPSIS
  Diagnostic script for CMRelocator against an IBM Content Manager v8
  CMIS 1.1 Browser Binding endpoint.

.DESCRIPTION
  Runs six checks end-to-end, prints every CMIS SQL statement it sends:
    1. Service document             - confirms repo + extracts repositoryUrl
    2. Source type definition       - queryName of type + CIF property
    3. Target type definition       - queryName of type + CIF property
    4. Source folders (no filter)   - sample, distinct CIFs seen
    5. Target folders (no filter)   - sample + source/target CIF overlap
    6. Lookup by CIF (if provided)  - tries both string and numeric literal,
                                      then lists documents IN_FOLDER of the
                                      first matching source folder.

  Property names with special characters (dots, hyphens) are accessed via
  PSObject.Properties indexer to avoid PowerShell's dotted-path parsing.

  Auth: Basic, sent preemptively (some CMIS servers do not issue a 401
  challenge so -Credential alone is not enough).

  Compatible with Windows PowerShell 5.1 and PowerShell 7+.

.PARAMETER ServiceUrl
  CMIS Browser Binding service URL, e.g. https://host:9443/cmis/browser

.PARAMETER Repository
  CMIS repository id (key in the service document).

.PARAMETER Username
  Username for HTTP Basic auth.

.PARAMETER Password
  SecureString. If omitted, you are prompted.

.PARAMETER SourceTypeId
  cmis:objectTypeId of the source folder type, e.g. '$p!-2_BAC_01_01_01_02v-1'.
  Use single quotes when passing on the command line so PowerShell does not
  expand the leading $.

.PARAMETER TargetTypeId
  cmis:objectTypeId of the target folder type.

.PARAMETER CifPropertyId
  Property id holding the CIF value. Default: 'clbNonGroup.BAC_CIF'.

.PARAMETER Cif
  Optional. A specific CIF value to test end-to-end.

.PARAMETER SkipCertCheck
  Bypass TLS validation. Use when the CM server has a self-signed cert.

.EXAMPLE
  .\Test-CMRelocator.ps1 `
    -ServiceUrl 'https://cmserver:9443/cmis/browser' `
    -Repository 'icmnlsdb_cmis' `
    -Username 'admin' `
    -SourceTypeId '$p!-2_BAC_01_01_01_02v-1' `
    -TargetTypeId '$p!-2_BAC_01_01_01_02v-2' `
    -CifPropertyId 'clbNonGroup.BAC_CIF' `
    -Cif '1195972' `
    -SkipCertCheck
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ServiceUrl,
    [Parameter(Mandatory)][string]$Repository,
    [Parameter(Mandatory)][string]$Username,
    [SecureString]$Password,
    [Parameter(Mandatory)][string]$SourceTypeId,
    [Parameter(Mandatory)][string]$TargetTypeId,
    [string]$CifPropertyId = "clbNonGroup.BAC_CIF",
    [string]$Cif,
    [switch]$SkipCertCheck
)

$ErrorActionPreference = 'Stop'

# ---------- pretty output ----------
function Write-Section([string]$title) {
    $bar = ('=' * 78)
    Write-Host ""
    Write-Host $bar -ForegroundColor Cyan
    Write-Host (" " + $title) -ForegroundColor Cyan
    Write-Host $bar -ForegroundColor Cyan
}
function Write-Ok([string]$m)   { Write-Host "[OK]   $m" -ForegroundColor Green }
function Write-WarnLine([string]$m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Write-Fail([string]$m) { Write-Host "[FAIL] $m" -ForegroundColor Red }
function Write-Info([string]$m) { Write-Host "[INFO] $m" -ForegroundColor Gray }
function Write-Sql([string]$s)  { Write-Host "       SQL: $s" -ForegroundColor DarkGray }

# ---------- password + auth ----------
if (-not $Password) {
    $Password = Read-Host -Prompt "Password for $Username" -AsSecureString
}
function ConvertFrom-SecureToPlain([SecureString]$ss) {
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss)
    try   { return [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
$plain = ConvertFrom-SecureToPlain $Password
$basic = [Convert]::ToBase64String(
    [System.Text.Encoding]::UTF8.GetBytes("${Username}:${plain}")
)
$authHeader = @{ Authorization = "Basic $basic" }
Remove-Variable plain

# ---------- TLS ----------
$isPS7 = $PSVersionTable.PSVersion.Major -ge 6
$extra = @{}
if ($SkipCertCheck) {
    if ($isPS7) {
        $extra['SkipCertificateCheck'] = $true
    } else {
        if (-not ([System.Management.Automation.PSTypeName]'TrustAllCertsPolicy').Type) {
            Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAllCertsPolicy : ICertificatePolicy {
    public bool CheckValidationResult(
        ServicePoint srvPoint, X509Certificate certificate,
        WebRequest request, int certificateProblem) { return true; }
}
"@
        }
        [System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllCertsPolicy
        [System.Net.ServicePointManager]::SecurityProtocol =
            [System.Net.SecurityProtocolType]::Tls12 -bor
            [System.Net.SecurityProtocolType]::Tls11 -bor
            [System.Net.SecurityProtocolType]::Tls
    }
}

$ServiceUrl = $ServiceUrl.TrimEnd('/')

# ---------- HTTP helpers ----------
function Invoke-CmisGet([string]$url, [hashtable]$query) {
    $p = @{
        Method  = 'GET'
        Uri     = $url
        Headers = $authHeader
    }
    if ($query) { $p['Body'] = $query }
    foreach ($k in $extra.Keys) { $p[$k] = $extra[$k] }
    return Invoke-RestMethod @p
}
function Invoke-CmisPost([string]$url, [hashtable]$form) {
    $p = @{
        Method      = 'POST'
        Uri         = $url
        Headers     = $authHeader
        Body        = $form
        ContentType = 'application/x-www-form-urlencoded'
    }
    foreach ($k in $extra.Keys) { $p[$k] = $extra[$k] }
    return Invoke-RestMethod @p
}

# ---------- PSObject property access (safe for keys with dots / hyphens) ----------
function Get-Member-Value($obj, [string]$name) {
    if ($null -eq $obj) { return $null }
    $prop = $obj.PSObject.Properties[$name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}
function Get-PropValue($row, [string]$key) {
    $p = Get-Member-Value $row 'properties'
    if ($p) {
        $entry = Get-Member-Value $p $key
        if ($entry) {
            $v = Get-Member-Value $entry 'value'
            if ($v -is [array]) { return $v[0] }
            return $v
        }
    }
    $s = Get-Member-Value $row 'succinctProperties'
    if ($s) {
        $v = Get-Member-Value $s $key
        if ($v -is [array]) { return $v[0] }
        return $v
    }
    return $null
}
function Get-CifValue($row, [string]$cifQN, [string]$cifId) {
    $v = Get-PropValue $row $cifQN
    if ($null -eq $v) { $v = Get-PropValue $row $cifId }
    return $v
}

# ====================================================================
# Step 1: Service document
# ====================================================================
Write-Section "Step 1: Service document"
Write-Info "GET $ServiceUrl"
try { $svc = Invoke-CmisGet $ServiceUrl $null }
catch { Write-Fail "GET service document failed: $($_.Exception.Message)"; exit 1 }

$repoInfo = Get-Member-Value $svc $Repository
if (-not $repoInfo) {
    Write-Fail "Repository '$Repository' not found in service document."
    Write-Info ("Available repositories: " + (($svc.PSObject.Properties.Name) -join ', '))
    exit 1
}
$repoUrl    = Get-Member-Value $repoInfo 'repositoryUrl'
$rootUrl    = Get-Member-Value $repoInfo 'rootFolderUrl'
$repoName   = Get-Member-Value $repoInfo 'repositoryName'
$prodName   = Get-Member-Value $repoInfo 'productName'
$prodVer    = Get-Member-Value $repoInfo 'productVersion'
Write-Ok ("Repository '{0}' = {1} ({2} {3})" -f $Repository, $repoName, $prodName, $prodVer)
Write-Info "repositoryUrl  = $repoUrl"
Write-Info "rootFolderUrl  = $rootUrl"

# ---------- type definition resolver ----------
function Resolve-QueryNames([string]$typeId) {
    $td = Invoke-CmisGet $repoUrl @{ cmisselector = 'typeDefinition'; typeId = $typeId }
    $typeQN = Get-Member-Value $td 'queryName'
    if (-not $typeQN) { throw "Type '$typeId' has no queryName in its definition." }
    $pdMap = Get-Member-Value $td 'propertyDefinitions'
    if (-not $pdMap) { throw "Type '$typeId' has no propertyDefinitions." }
    $pdef = Get-Member-Value $pdMap $CifPropertyId
    if (-not $pdef) {
        # Try matching by inner .id (CMIS allows different keying schemes)
        foreach ($p in $pdMap.PSObject.Properties) {
            $cand = $p.Value
            if ((Get-Member-Value $cand 'id') -eq $CifPropertyId) { $pdef = $cand; break }
        }
    }
    if (-not $pdef) {
        $sample = ($pdMap.PSObject.Properties.Name | Select-Object -First 25) -join ', '
        throw "Property '$CifPropertyId' not on type '$typeId'. First 25 property ids: $sample"
    }
    $propQN = Get-Member-Value $pdef 'queryName'
    if (-not $propQN) { throw "Property '$CifPropertyId' on type '$typeId' has no queryName." }
    return @{ TypeQN = $typeQN; PropQN = $propQN }
}

# ====================================================================
# Step 2: Source type definition
# ====================================================================
Write-Section "Step 2: Source type definition  ($SourceTypeId)"
try {
    $src = Resolve-QueryNames $SourceTypeId
    Write-Ok ("Source type queryName  = {0}" -f $src.TypeQN)
    Write-Ok ("CIF property queryName = {0}" -f $src.PropQN)
} catch { Write-Fail $_.Exception.Message; exit 1 }

# ====================================================================
# Step 3: Target type definition
# ====================================================================
Write-Section "Step 3: Target type definition  ($TargetTypeId)"
try {
    $tgt = Resolve-QueryNames $TargetTypeId
    Write-Ok ("Target type queryName  = {0}" -f $tgt.TypeQN)
    Write-Ok ("CIF property queryName = {0}" -f $tgt.PropQN)
} catch { Write-Fail $_.Exception.Message; exit 1 }

# ---------- folder listing helper ----------
function Query-Folders([string]$typeQN, [string]$cifQN, [int]$maxItems = 50) {
    $stmt = "SELECT cmis:objectId, cmis:name, $cifQN FROM $typeQN"
    Write-Sql $stmt
    return Invoke-CmisPost $repoUrl @{
        cmisaction        = 'query'
        statement         = $stmt
        searchAllVersions = 'false'
        maxItems          = $maxItems
        skipCount         = 0
    }
}
function Show-FolderSample($res, [string]$cifQN, [string]$label) {
    $rows = @(Get-Member-Value $res 'results')
    $num  = Get-Member-Value $res 'numItems'
    if ($rows.Count -eq 0) {
        Write-WarnLine "$label folders returned: 0 (numItems=$num)"
        return @()
    }
    Write-Ok "$label folders returned: $($rows.Count) (numItems=$num)"
    $sample = foreach ($r in $rows) {
        [pscustomobject]@{
            CIF      = Get-CifValue $r $cifQN $CifPropertyId
            Name     = Get-PropValue $r 'cmis:name'
            ObjectId = Get-PropValue $r 'cmis:objectId'
        }
    }
    $sample | Select-Object -First 10 | Format-Table -AutoSize | Out-String | Write-Host
    return $sample
}

# ====================================================================
# Step 4: Source folders sample
# ====================================================================
Write-Section "Step 4: Source folders (no filter, first 50)"
try {
    $srcRes  = Query-Folders $src.TypeQN $src.PropQN 50
    $srcRows = Show-FolderSample $srcRes $src.PropQN "Source"
} catch { Write-Fail $_.Exception.Message; exit 1 }

# ====================================================================
# Step 5: Target folders sample + overlap
# ====================================================================
Write-Section "Step 5: Target folders (no filter, first 50)"
try {
    $tgtRes  = Query-Folders $tgt.TypeQN $tgt.PropQN 50
    $tgtRows = Show-FolderSample $tgtRes $tgt.PropQN "Target"
} catch { Write-Fail $_.Exception.Message; exit 1 }

Write-Section "Step 5b: Source-target CIF overlap (in samples)"
$srcCifs = $srcRows | Where-Object { $_.CIF } | ForEach-Object { "$($_.CIF)" } | Sort-Object -Unique
$tgtCifs = $tgtRows | Where-Object { $_.CIF } | ForEach-Object { "$($_.CIF)" } | Sort-Object -Unique
$overlap = @($srcCifs | Where-Object { $tgtCifs -contains $_ })
Write-Info "Distinct source CIFs: $($srcCifs.Count)"
Write-Info "Distinct target CIFs: $($tgtCifs.Count)"
if ($overlap.Count -eq 0) {
    Write-WarnLine "No overlapping CIFs in first 50 of each. Sample may be too small, or types do not share customers."
} else {
    $first = ($overlap | Select-Object -First 5) -join ', '
    Write-Ok "Overlapping CIFs (sample): $($overlap.Count). First few: $first"
}

# ====================================================================
# Step 6: Specific CIF lookup
# ====================================================================
if ($Cif) {
    Write-Section "Step 6: Lookup for CIF = $Cif"
    $found = $false
    foreach ($mode in @('string','numeric')) {
        $literal = if ($mode -eq 'string') { "'$Cif'" } else { $Cif }
        Write-Host ""
        Write-Info "Trying $mode literal: $literal"

        $stmt = "SELECT cmis:objectId, cmis:name FROM $($src.TypeQN) WHERE $($src.PropQN) = $literal"
        Write-Sql $stmt
        try {
            $r = Invoke-CmisPost $repoUrl @{
                cmisaction = 'query'; statement = $stmt; maxItems = 5; skipCount = 0
            }
        } catch {
            Write-Fail "Source query failed: $($_.Exception.Message)"
            continue
        }
        $rowsS = @(Get-Member-Value $r 'results')
        if ($rowsS.Count -eq 0) {
            Write-WarnLine "Source returned 0 folders with $mode literal."
            continue
        }
        Write-Ok "Source matched $($rowsS.Count) folder(s) using $mode CIF."
        $srcFolder = Get-PropValue $rowsS[0] 'cmis:objectId'
        Write-Info "First source objectId: $srcFolder"

        $stmt2 = "SELECT cmis:objectId, cmis:name FROM $($tgt.TypeQN) WHERE $($tgt.PropQN) = $literal"
        Write-Sql $stmt2
        try {
            $r2 = Invoke-CmisPost $repoUrl @{
                cmisaction = 'query'; statement = $stmt2; maxItems = 5; skipCount = 0
            }
        } catch {
            Write-Fail "Target query failed: $($_.Exception.Message)"
            continue
        }
        $rowsT = @(Get-Member-Value $r2 'results')
        if ($rowsT.Count -eq 0) {
            Write-WarnLine "Target has NO folder for CIF $Cif (using $mode literal). This CIF would be skipped."
            $found = $true
            break
        }
        Write-Ok "Target matched $($rowsT.Count) folder(s) using $mode CIF."
        $tgtFolder = Get-PropValue $rowsT[0] 'cmis:objectId'
        Write-Info "First target objectId: $tgtFolder"

        # Documents IN_FOLDER source
        Write-Section "Step 6b: Documents IN_FOLDER source"
        $escFolder = $srcFolder -replace "'", "''"
        $stmt3 = "SELECT cmis:objectId, cmis:name FROM cmis:document WHERE IN_FOLDER('$escFolder')"
        Write-Sql $stmt3
        try {
            $r3 = Invoke-CmisPost $repoUrl @{
                cmisaction = 'query'; statement = $stmt3; maxItems = 20; skipCount = 0
            }
        } catch {
            Write-Fail "Document listing failed: $($_.Exception.Message)"
            $found = $true
            break
        }
        $rowsD = @(Get-Member-Value $r3 'results')
        if ($rowsD.Count -eq 0) {
            Write-WarnLine "Source folder contains 0 documents (nothing to migrate)."
        } else {
            Write-Ok "Source folder contains $($rowsD.Count) document(s):"
            $rowsD | Select-Object -First 20 | ForEach-Object {
                "  - {0,-40}  [{1}]" -f (Get-PropValue $_ 'cmis:name'), (Get-PropValue $_ 'cmis:objectId') |
                    Write-Host
            }
        }
        $found = $true
        break
    }
    if (-not $found) {
        Write-WarnLine "No source folder matched CIF $Cif in either string or numeric literal mode."
        Write-Info "Possible causes: typo, CIF stored under a different property, or CIF not in this type."
    }
}

# ====================================================================
# Hints
# ====================================================================
Write-Section "Interpretation guide"
Write-Info "Step 4 = 0 folders                  -> SourceTypeId is wrong, or no instances exist."
Write-Info "Step 5 = 0 folders                  -> TargetTypeId is wrong, or no instances exist."
Write-Info "Step 5b overlap = 0                 -> Source and target types do not share CIFs (small sample? wrong types?)"
Write-Info "Step 6 string fails, numeric works  -> CIF column is numeric; the TUI must send unquoted literals."
Write-Info "Step 6 both fail                    -> The CIF you typed does not exist on the source type, or wrong property."
