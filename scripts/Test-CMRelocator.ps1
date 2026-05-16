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

  Helpers (Section/Ok/Warn/Fail/Info/Sql/CmisGet/CmisPost/MemberValue/...)
  use hyphen-less names so PowerShell resolves them strictly as
  script-local functions and avoids cmdlet-resolver conflicts.

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

# ---------- pretty output (hyphen-less to avoid cmdlet resolver) ----------
function Section([string]$title) {
    $bar = ('=' * 78)
    Write-Host ""
    Write-Host $bar -ForegroundColor Cyan
    Write-Host (" " + $title) -ForegroundColor Cyan
    Write-Host $bar -ForegroundColor Cyan
}
function Ok([string]$m)   { Write-Host "[OK]   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "[FAIL] $m" -ForegroundColor Red }
function Info([string]$m) { Write-Host "[INFO] $m" -ForegroundColor Gray }
function Sql([string]$s)  { Write-Host "       SQL: $s" -ForegroundColor DarkGray }

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
function CmisGet([string]$url, [hashtable]$query) {
    $p = @{
        Method  = 'GET'
        Uri     = $url
        Headers = $authHeader
    }
    if ($query) { $p['Body'] = $query }
    foreach ($k in $extra.Keys) { $p[$k] = $extra[$k] }
    return Invoke-RestMethod @p
}
function CmisPost([string]$url, [hashtable]$form) {
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
function MemberValue($obj, [string]$name) {
    if ($null -eq $obj) { return $null }
    $prop = $obj.PSObject.Properties[$name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}
function PropValue($row, [string]$key) {
    $p = MemberValue $row 'properties'
    if ($p) {
        $entry = MemberValue $p $key
        if ($entry) {
            $v = MemberValue $entry 'value'
            if ($v -is [array]) { return $v[0] }
            return $v
        }
    }
    $s = MemberValue $row 'succinctProperties'
    if ($s) {
        $v = MemberValue $s $key
        if ($v -is [array]) { return $v[0] }
        return $v
    }
    return $null
}
function CifValue($row, [string]$cifQN, [string]$cifId) {
    $v = PropValue $row $cifQN
    if ($null -eq $v) { $v = PropValue $row $cifId }
    return $v
}

# ====================================================================
# Step 1: Service document
# ====================================================================
Section "Step 1: Service document"
Info "GET $ServiceUrl"
try { $svc = CmisGet $ServiceUrl $null }
catch { Fail "GET service document failed: $($_.Exception.Message)"; exit 1 }

$repoInfo = MemberValue $svc $Repository
if (-not $repoInfo) {
    Fail "Repository '$Repository' not found in service document."
    Info ("Available repositories: " + (($svc.PSObject.Properties.Name) -join ', '))
    exit 1
}
$repoUrl    = MemberValue $repoInfo 'repositoryUrl'
$rootUrl    = MemberValue $repoInfo 'rootFolderUrl'
$repoName   = MemberValue $repoInfo 'repositoryName'
$prodName   = MemberValue $repoInfo 'productName'
$prodVer    = MemberValue $repoInfo 'productVersion'
Ok ("Repository '{0}' = {1} ({2} {3})" -f $Repository, $repoName, $prodName, $prodVer)
Info "repositoryUrl  = $repoUrl"
Info "rootFolderUrl  = $rootUrl"

# ---------- type definition resolver ----------
function ResolveQueryNames([string]$typeId) {
    $td = CmisGet $repoUrl @{ cmisselector = 'typeDefinition'; typeId = $typeId }
    $typeQN = MemberValue $td 'queryName'
    if (-not $typeQN) { throw "Type '$typeId' has no queryName in its definition." }
    $pdMap = MemberValue $td 'propertyDefinitions'
    if (-not $pdMap) { throw "Type '$typeId' has no propertyDefinitions." }
    $pdef = MemberValue $pdMap $CifPropertyId
    if (-not $pdef) {
        # Try matching by inner .id (CMIS allows different keying schemes)
        foreach ($p in $pdMap.PSObject.Properties) {
            $cand = $p.Value
            if ((MemberValue $cand 'id') -eq $CifPropertyId) { $pdef = $cand; break }
        }
    }
    if (-not $pdef) {
        $sample = ($pdMap.PSObject.Properties.Name | Select-Object -First 25) -join ', '
        throw "Property '$CifPropertyId' not on type '$typeId'. First 25 property ids: $sample"
    }
    $propQN = MemberValue $pdef 'queryName'
    if (-not $propQN) { throw "Property '$CifPropertyId' on type '$typeId' has no queryName." }
    return @{ TypeQN = $typeQN; PropQN = $propQN }
}

# ====================================================================
# Step 2: Source type definition
# ====================================================================
Section "Step 2: Source type definition  ($SourceTypeId)"
try {
    $src = ResolveQueryNames $SourceTypeId
    Ok ("Source type queryName  = {0}" -f $src.TypeQN)
    Ok ("CIF property queryName = {0}" -f $src.PropQN)
} catch { Fail $_.Exception.Message; exit 1 }

# ====================================================================
# Step 3: Target type definition
# ====================================================================
Section "Step 3: Target type definition  ($TargetTypeId)"
try {
    $tgt = ResolveQueryNames $TargetTypeId
    Ok ("Target type queryName  = {0}" -f $tgt.TypeQN)
    Ok ("CIF property queryName = {0}" -f $tgt.PropQN)
} catch { Fail $_.Exception.Message; exit 1 }

# ---------- folder listing helper ----------
function QueryFolders([string]$typeQN, [string]$cifQN, [int]$maxItems = 50) {
    $stmt = "SELECT cmis:objectId, cmis:name, $cifQN FROM $typeQN"
    Sql $stmt
    return CmisPost $repoUrl @{
        cmisaction        = 'query'
        statement         = $stmt
        searchAllVersions = 'false'
        maxItems          = $maxItems
        skipCount         = 0
    }
}
function ShowFolderSample($res, [string]$cifQN, [string]$label) {
    $rows = @(MemberValue $res 'results')
    $num  = MemberValue $res 'numItems'
    if ($rows.Count -eq 0) {
        Warn "$label folders returned: 0 (numItems=$num)"
        return @()
    }
    Ok "$label folders returned: $($rows.Count) (numItems=$num)"
    $sample = foreach ($r in $rows) {
        [pscustomobject]@{
            CIF      = CifValue $r $cifQN $CifPropertyId
            Name     = PropValue $r 'cmis:name'
            ObjectId = PropValue $r 'cmis:objectId'
        }
    }
    $sample | Select-Object -First 10 | Format-Table -AutoSize | Out-String | Write-Host
    return $sample
}

# ====================================================================
# Step 4: Source folders sample
# ====================================================================
Section "Step 4: Source folders (no filter, first 50)"
try {
    $srcRes  = QueryFolders $src.TypeQN $src.PropQN 50
    $srcRows = ShowFolderSample $srcRes $src.PropQN "Source"
} catch { Fail $_.Exception.Message; exit 1 }

# ====================================================================
# Step 5: Target folders sample + overlap
# ====================================================================
Section "Step 5: Target folders (no filter, first 50)"
try {
    $tgtRes  = QueryFolders $tgt.TypeQN $tgt.PropQN 50
    $tgtRows = ShowFolderSample $tgtRes $tgt.PropQN "Target"
} catch { Fail $_.Exception.Message; exit 1 }

Section "Step 5b: Source-target CIF overlap (in samples)"
$srcCifs = $srcRows | Where-Object { $_.CIF } | ForEach-Object { "$($_.CIF)" } | Sort-Object -Unique
$tgtCifs = $tgtRows | Where-Object { $_.CIF } | ForEach-Object { "$($_.CIF)" } | Sort-Object -Unique
$overlap = @($srcCifs | Where-Object { $tgtCifs -contains $_ })
Info "Distinct source CIFs: $($srcCifs.Count)"
Info "Distinct target CIFs: $($tgtCifs.Count)"
if ($overlap.Count -eq 0) {
    Warn "No overlapping CIFs in first 50 of each. Sample may be too small, or types do not share customers."
} else {
    $first = ($overlap | Select-Object -First 5) -join ', '
    Ok "Overlapping CIFs (sample): $($overlap.Count). First few: $first"
}

# ====================================================================
# Step 6: Specific CIF lookup
# ====================================================================
if ($Cif) {
    Section "Step 6: Lookup for CIF = $Cif"
    $found = $false
    foreach ($mode in @('string','numeric')) {
        $literal = if ($mode -eq 'string') { "'$Cif'" } else { $Cif }
        Write-Host ""
        Info "Trying $mode literal: $literal"

        $stmt = "SELECT cmis:objectId, cmis:name FROM $($src.TypeQN) WHERE $($src.PropQN) = $literal"
        Sql $stmt
        try {
            $r = CmisPost $repoUrl @{
                cmisaction = 'query'; statement = $stmt; maxItems = 5; skipCount = 0
            }
        } catch {
            Fail "Source query failed: $($_.Exception.Message)"
            continue
        }
        $rowsS = @(MemberValue $r 'results')
        if ($rowsS.Count -eq 0) {
            Warn "Source returned 0 folders with $mode literal."
            continue
        }
        Ok "Source matched $($rowsS.Count) folder(s) using $mode CIF."
        $srcFolder = PropValue $rowsS[0] 'cmis:objectId'
        Info "First source objectId: $srcFolder"

        $stmt2 = "SELECT cmis:objectId, cmis:name FROM $($tgt.TypeQN) WHERE $($tgt.PropQN) = $literal"
        Sql $stmt2
        try {
            $r2 = CmisPost $repoUrl @{
                cmisaction = 'query'; statement = $stmt2; maxItems = 5; skipCount = 0
            }
        } catch {
            Fail "Target query failed: $($_.Exception.Message)"
            continue
        }
        $rowsT = @(MemberValue $r2 'results')
        if ($rowsT.Count -eq 0) {
            Warn "Target has NO folder for CIF $Cif (using $mode literal). This CIF would be skipped."
            $found = $true
            break
        }
        Ok "Target matched $($rowsT.Count) folder(s) using $mode CIF."
        $tgtFolder = PropValue $rowsT[0] 'cmis:objectId'
        Info "First target objectId: $tgtFolder"

        # Documents IN_FOLDER source
        Section "Step 6b: Documents IN_FOLDER source"
        $escFolder = $srcFolder -replace "'", "''"
        $stmt3 = "SELECT cmis:objectId, cmis:name FROM cmis:document WHERE IN_FOLDER('$escFolder')"
        Sql $stmt3
        try {
            $r3 = CmisPost $repoUrl @{
                cmisaction = 'query'; statement = $stmt3; maxItems = 20; skipCount = 0
            }
        } catch {
            Fail "Document listing failed: $($_.Exception.Message)"
            $found = $true
            break
        }
        $rowsD = @(MemberValue $r3 'results')
        if ($rowsD.Count -eq 0) {
            Warn "Source folder contains 0 documents (nothing to migrate)."
        } else {
            Ok "Source folder contains $($rowsD.Count) document(s):"
            $rowsD | Select-Object -First 20 | ForEach-Object {
                "  - {0,-40}  [{1}]" -f (PropValue $_ 'cmis:name'), (PropValue $_ 'cmis:objectId') |
                    Write-Host
            }
        }
        $found = $true
        break
    }
    if (-not $found) {
        Warn "No source folder matched CIF $Cif in either string or numeric literal mode."
        Info "Possible causes: typo, CIF stored under a different property, or CIF not in this type."
    }
}

# ====================================================================
# Hints
# ====================================================================
Section "Interpretation guide"
Info "Step 4 = 0 folders                  -> SourceTypeId is wrong, or no instances exist."
Info "Step 5 = 0 folders                  -> TargetTypeId is wrong, or no instances exist."
Info "Step 5b overlap = 0                 -> Source and target types do not share CIFs (small sample? wrong types?)"
Info "Step 6 string fails, numeric works  -> CIF column is numeric; the TUI must send unquoted literals."
Info "Step 6 both fail                    -> The CIF you typed does not exist on the source type, or wrong property."
