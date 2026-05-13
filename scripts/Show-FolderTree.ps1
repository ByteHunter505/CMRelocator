<#
.SYNOPSIS
  For a given CIF, resolves the source and target folders in IBM CM v8 via
  CMIS and prints the full folder tree of each side, plus a comparison of
  top-level subfolder names with a strategy recommendation.

.DESCRIPTION
  Walks each tree recursively (subject to -MaxDepth) and shows, for every
  folder, the number of documents it contains directly. Optionally lists
  document names inside each folder with -ShowDocs.

  Useful to decide between three migration strategies:
    A. Move-as-unit (move source's direct children into target)
    B. Merge by name (align same-named subfolders, move docs into them)
    C. Flatten (move every descendant doc directly into target)

  Authentication: preemptive HTTP Basic.
  TLS: -SkipCertCheck bypasses validation for self-signed certs.
  Compatible with Windows PowerShell 5.1 and PowerShell 7+.

.PARAMETER ServiceUrl
  CMIS Browser Binding service URL, e.g. https://host:9443/cmis/browser

.PARAMETER Repository
  CMIS repository id.

.PARAMETER Username
  Username for HTTP Basic auth.

.PARAMETER Password
  SecureString. Prompted if not provided.

.PARAMETER SourceTypeId
  cmis:objectTypeId of the source folder type. Use single quotes on the
  command line so PowerShell does not expand the leading $.

.PARAMETER TargetTypeId
  cmis:objectTypeId of the target folder type.

.PARAMETER CifPropertyId
  Property id holding the CIF value. Default: 'clbNonGroup.BAC_CIF'.

.PARAMETER Cif
  REQUIRED. The CIF whose source and target folders should be inspected.

.PARAMETER MaxDepth
  Max recursion depth when walking each tree. Default 4.

.PARAMETER ShowDocs
  If set, lists document names inside each folder (else only counts).

.PARAMETER SkipCertCheck
  Bypass TLS validation.

.EXAMPLE
  .\Show-FolderTree.ps1 `
    -ServiceUrl 'https://cmserver:9443/cmis/browser' `
    -Repository 'icmnlsdb_cmis' `
    -Username 'admin' `
    -SourceTypeId '$p!-2_BAC_01_01_01_02v-1' `
    -TargetTypeId '$p!-2_BAC_01_01_01_02v-2' `
    -Cif '1195972' `
    -ShowDocs `
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
    [Parameter(Mandatory)][string]$Cif,
    [int]$MaxDepth = 4,
    [switch]$ShowDocs,
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
function Write-Ok([string]$m)       { Write-Host "[OK]   $m" -ForegroundColor Green }
function Write-WarnLine([string]$m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Write-Fail([string]$m)     { Write-Host "[FAIL] $m" -ForegroundColor Red }
function Write-Info([string]$m)     { Write-Host "[INFO] $m" -ForegroundColor Gray }

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
    $p = @{ Method = 'GET'; Uri = $url; Headers = $authHeader }
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

# ---------- PSObject property access (safe for dotted/hyphenated keys) ----------
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

# ====================================================================
# Step 1: Service document
# ====================================================================
Write-Section "Service document"
Write-Info "GET $ServiceUrl"
try { $svc = Invoke-CmisGet $ServiceUrl $null }
catch { Write-Fail "GET service document failed: $($_.Exception.Message)"; exit 1 }
$repoInfo = Get-Member-Value $svc $Repository
if (-not $repoInfo) {
    Write-Fail "Repository '$Repository' not found."
    Write-Info ("Available: " + (($svc.PSObject.Properties.Name) -join ', '))
    exit 1
}
$repoUrl = Get-Member-Value $repoInfo 'repositoryUrl'
Write-Ok "Repository '$Repository' resolved. repositoryUrl = $repoUrl"

# ---------- queryName resolver ----------
function Resolve-QueryNames([string]$typeId) {
    $td = Invoke-CmisGet $repoUrl @{ cmisselector = 'typeDefinition'; typeId = $typeId }
    $typeQN = Get-Member-Value $td 'queryName'
    if (-not $typeQN) { throw "Type '$typeId' has no queryName." }
    $pdMap = Get-Member-Value $td 'propertyDefinitions'
    if (-not $pdMap) { throw "Type '$typeId' has no propertyDefinitions." }
    $pdef = Get-Member-Value $pdMap $CifPropertyId
    if (-not $pdef) {
        foreach ($p in $pdMap.PSObject.Properties) {
            $cand = $p.Value
            if ((Get-Member-Value $cand 'id') -eq $CifPropertyId) { $pdef = $cand; break }
        }
    }
    if (-not $pdef) {
        $sample = ($pdMap.PSObject.Properties.Name | Select-Object -First 25) -join ', '
        throw "Property '$CifPropertyId' not on type '$typeId'. First 25: $sample"
    }
    $propQN = Get-Member-Value $pdef 'queryName'
    if (-not $propQN) { throw "Property '$CifPropertyId' has no queryName." }
    return @{ TypeQN = $typeQN; PropQN = $propQN }
}

# ---------- find the root folder for a CIF on one side ----------
function Find-RootFolder([hashtable]$qn, [string]$cif, [string]$label) {
    foreach ($mode in @('string','numeric')) {
        $literal = if ($mode -eq 'string') { "'$cif'" } else { $cif }
        $stmt = "SELECT cmis:objectId, cmis:name FROM $($qn.TypeQN) WHERE $($qn.PropQN) = $literal"
        try {
            $r = Invoke-CmisPost $repoUrl @{
                cmisaction = 'query'; statement = $stmt; maxItems = 5; skipCount = 0
            }
        } catch {
            Write-WarnLine "$label query ($mode) failed: $($_.Exception.Message)"
            continue
        }
        $rows = @(Get-Member-Value $r 'results')
        if ($rows.Count -eq 0) { continue }
        if ($rows.Count -gt 1) {
            Write-WarnLine "$label: multiple folders for CIF $cif (using first; 1:1 assumption)."
        }
        $id   = Get-PropValue $rows[0] 'cmis:objectId'
        $name = Get-PropValue $rows[0] 'cmis:name'
        Write-Ok ("$label root folder: '{0}'  objectId={1}  (cif literal: {2})" -f $name, $id, $mode)
        return @{ ObjectId = $id; Name = $name }
    }
    return $null
}

# ====================================================================
# Step 2: Resolve queryNames for both types
# ====================================================================
Write-Section "Type definitions"
try {
    $src = Resolve-QueryNames $SourceTypeId
    Write-Ok ("Source type queryName  = {0}   (CIF property queryName = {1})" -f $src.TypeQN, $src.PropQN)
} catch { Write-Fail $_.Exception.Message; exit 1 }
try {
    $tgt = Resolve-QueryNames $TargetTypeId
    Write-Ok ("Target type queryName  = {0}   (CIF property queryName = {1})" -f $tgt.TypeQN, $tgt.PropQN)
} catch { Write-Fail $_.Exception.Message; exit 1 }

# ====================================================================
# Step 3: Resolve root folders for the CIF
# ====================================================================
Write-Section "Root folders for CIF $Cif"
$srcRoot = Find-RootFolder $src $Cif "SOURCE"
$tgtRoot = Find-RootFolder $tgt $Cif "TARGET"
if (-not $srcRoot) { Write-Fail "Source folder not found for CIF $Cif. Aborting."; exit 1 }
if (-not $tgtRoot) { Write-Fail "Target folder not found for CIF $Cif. Aborting."; exit 1 }

# ====================================================================
# Tree walking
# ====================================================================
$script:topLevelSrcNames = New-Object System.Collections.Generic.List[string]
$script:topLevelTgtNames = New-Object System.Collections.Generic.List[string]

function Get-FolderChildren([string]$folderId) {
    $escId = $folderId.Replace("'", "''")
    $stmt = "SELECT cmis:objectId, cmis:name FROM cmis:folder WHERE IN_FOLDER('$escId')"
    $r = Invoke-CmisPost $repoUrl @{
        cmisaction = 'query'; statement = $stmt; maxItems = 500; skipCount = 0
    }
    return @(Get-Member-Value $r 'results')
}

function Get-FolderDocuments([string]$folderId, [int]$maxItems = 500) {
    $escId = $folderId.Replace("'", "''")
    $stmt = "SELECT cmis:objectId, cmis:name FROM cmis:document WHERE IN_FOLDER('$escId')"
    $r = Invoke-CmisPost $repoUrl @{
        cmisaction = 'query'; statement = $stmt; maxItems = $maxItems; skipCount = 0
    }
    $rows = @(Get-Member-Value $r 'results')
    $num  = Get-Member-Value $r 'numItems'
    if ($null -eq $num) { $num = $rows.Count }
    return @{ Docs = $rows; NumItems = $num }
}

function Show-Tree([string]$folderId, [string]$folderName, [int]$depth, [string]$prefix, [System.Collections.Generic.List[string]]$collectTopLevelInto) {
    if ($depth -gt $MaxDepth) {
        Write-Host "$prefix... (max depth reached, raise -MaxDepth to dig deeper)" -ForegroundColor DarkGray
        return
    }

    $docs = Get-FolderDocuments $folderId 500
    $children = Get-FolderChildren $folderId

    $childCount = $children.Count
    $docCount   = $docs.NumItems

    # Self line (only at root because parent already printed us)
    if ($depth -eq 0) {
        Write-Host ("{0}{1}/   [{2} subfolders, {3} docs]" -f $prefix, $folderName, $childCount, $docCount) -ForegroundColor White
    }

    # Print direct documents under this folder
    if ($ShowDocs -and $docs.Docs.Count -gt 0) {
        foreach ($d in $docs.Docs) {
            $dn = Get-PropValue $d 'cmis:name'
            Write-Host ("{0}  [D] {1}" -f $prefix, $dn) -ForegroundColor Gray
        }
    }

    # Recurse into subfolders
    foreach ($c in $children) {
        $cid   = Get-PropValue $c 'cmis:objectId'
        $cname = Get-PropValue $c 'cmis:name'
        if ($null -ne $collectTopLevelInto -and $depth -eq 0) {
            [void]$collectTopLevelInto.Add([string]$cname)
        }
        $cDocs = Get-FolderDocuments $cid 1
        $cCounters = "[{0} docs]" -f $cDocs.NumItems
        Write-Host ("{0}  [F] {1}/   {2}" -f $prefix, $cname, $cCounters) -ForegroundColor Cyan
        Show-Tree $cid $cname ($depth + 1) ($prefix + '    ') $null
    }
}

# ====================================================================
# Step 4: Print source tree
# ====================================================================
Write-Section ("SOURCE TREE  (CIF {0})" -f $Cif)
Show-Tree $srcRoot.ObjectId $srcRoot.Name 0 "" $script:topLevelSrcNames

# ====================================================================
# Step 5: Print target tree
# ====================================================================
Write-Section ("TARGET TREE  (CIF {0})" -f $Cif)
Show-Tree $tgtRoot.ObjectId $tgtRoot.Name 0 "" $script:topLevelTgtNames

# ====================================================================
# Step 6: Compare top-level subfolder names + recommend strategy
# ====================================================================
Write-Section "Top-level subfolder comparison"

$srcSet = @($script:topLevelSrcNames | Where-Object { $_ } | Sort-Object -Unique)
$tgtSet = @($script:topLevelTgtNames | Where-Object { $_ } | Sort-Object -Unique)
$inBoth  = @($srcSet | Where-Object { $tgtSet -contains $_ })
$onlySrc = @($srcSet | Where-Object { $tgtSet -notcontains $_ })
$onlyTgt = @($tgtSet | Where-Object { $srcSet -notcontains $_ })

Write-Host ("Source subfolders ({0}):" -f $srcSet.Count) -ForegroundColor White
if ($srcSet.Count -eq 0) { Write-Host "  (none)" -ForegroundColor DarkGray } else { $srcSet | ForEach-Object { "  - $_" | Write-Host } }
Write-Host ""
Write-Host ("Target subfolders ({0}):" -f $tgtSet.Count) -ForegroundColor White
if ($tgtSet.Count -eq 0) { Write-Host "  (none)" -ForegroundColor DarkGray } else { $tgtSet | ForEach-Object { "  - $_" | Write-Host } }

Write-Host ""
Write-Host ("In BOTH        ({0}):  {1}" -f $inBoth.Count,  ($inBoth -join ', '))  -ForegroundColor Green
Write-Host ("Only in SOURCE ({0}):  {1}" -f $onlySrc.Count, ($onlySrc -join ', ')) -ForegroundColor Yellow
Write-Host ("Only in TARGET ({0}):  {1}" -f $onlyTgt.Count, ($onlyTgt -join ', ')) -ForegroundColor Yellow

# ====================================================================
# Step 7: Recommendation
# ====================================================================
Write-Section "Strategy recommendation"

if ($srcSet.Count -eq 0) {
    Write-Info "Source has no subfolders. Use strategy A or C (whichever fits)."
    Write-Info "Direct documents under source root can be moved with the current TUI flow if any exist."
}
elseif ($tgtSet.Count -eq 0) {
    Write-Info "Target is empty (no subfolders)."
    Write-Info "Recommended: A  (move source's direct children into target; subtree comes along)."
}
elseif ($onlySrc.Count -eq 0 -and $inBoth.Count -gt 0) {
    Write-Info "All source subfolders also exist in target."
    Write-Info "Recommended: B  (merge by name: move docs of each source subfolder into the same-named target subfolder)."
}
elseif ($inBoth.Count -gt 0 -and $onlySrc.Count -gt 0) {
    Write-Info "Partial overlap. Some source subfolders exist in target, others do not."
    Write-Info "Recommended: B  (merge by name; create the missing subfolders in target, or move them as units)."
}
else {
    Write-Info "No overlap between source and target subfolder names."
    Write-Info "Recommended: A  (move source's children into target as-is; you'll end up with both sets of subfolders side by side)."
}

Write-Host ""
Write-Info "Reminder: strategies"
Write-Info "  A = move source's direct children into target (preserves subtrees, may duplicate same-named folders)"
Write-Info "  B = merge by name: align same-named subfolders, move docs into them, create the missing ones"
Write-Info "  C = flatten: IN_TREE(source) WHERE cmis:document, move every descendant doc into target root"
