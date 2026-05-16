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

  Helpers (Section/Ok/Warn/Fail/Info) use hyphen-less names so PowerShell
  resolves them strictly as script-local functions, never via the cmdlet
  resolver. This avoids "command not recognized" errors caused by stray
  module imports that interfere with hyphenated names.

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

# ---------- pretty output (hyphen-less to avoid cmdlet resolver) ----------
function Section([string]$t) {
    $bar = ('=' * 78)
    Write-Host ""
    Write-Host $bar -ForegroundColor Cyan
    Write-Host (" " + $t) -ForegroundColor Cyan
    Write-Host $bar -ForegroundColor Cyan
}
function Ok($m)   { Write-Host "[OK]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red }
function Info($m) { Write-Host "[INFO] $m" -ForegroundColor Gray }

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
    $p = @{ Method = 'GET'; Uri = $url; Headers = $authHeader }
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

# ---------- PSObject property access (safe for dotted/hyphenated keys) ----------
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

# ====================================================================
# Step 1: Service document
# ====================================================================
Section "Service document"
Info "GET $ServiceUrl"
try { $svc = CmisGet $ServiceUrl $null }
catch { Fail "GET service document failed: $($_.Exception.Message)"; exit 1 }
$repoInfo = MemberValue $svc $Repository
if (-not $repoInfo) {
    Fail "Repository '$Repository' not found."
    Info ("Available: " + (($svc.PSObject.Properties.Name) -join ', '))
    exit 1
}
$repoUrl = MemberValue $repoInfo 'repositoryUrl'
Ok "Repository '$Repository' resolved. repositoryUrl = $repoUrl"

# ---------- queryName resolver ----------
function ResolveQueryNames([string]$typeId) {
    $td = CmisGet $repoUrl @{ cmisselector = 'typeDefinition'; typeId = $typeId }
    $typeQN = MemberValue $td 'queryName'
    if (-not $typeQN) { throw "Type '$typeId' has no queryName." }
    $pdMap = MemberValue $td 'propertyDefinitions'
    if (-not $pdMap) { throw "Type '$typeId' has no propertyDefinitions." }
    $pdef = MemberValue $pdMap $CifPropertyId
    if (-not $pdef) {
        foreach ($p in $pdMap.PSObject.Properties) {
            $cand = $p.Value
            if ((MemberValue $cand 'id') -eq $CifPropertyId) { $pdef = $cand; break }
        }
    }
    if (-not $pdef) {
        $sample = ($pdMap.PSObject.Properties.Name | Select-Object -First 25) -join ', '
        throw "Property '$CifPropertyId' not on type '$typeId'. First 25: $sample"
    }
    $propQN = MemberValue $pdef 'queryName'
    if (-not $propQN) { throw "Property '$CifPropertyId' has no queryName." }
    return @{ TypeQN = $typeQN; PropQN = $propQN }
}

# ---------- find the root folder for a CIF on one side ----------
function FindRootFolder([hashtable]$qn, [string]$cif, [string]$label) {
    foreach ($mode in @('string','numeric')) {
        $literal = if ($mode -eq 'string') { "'$cif'" } else { $cif }
        $stmt = "SELECT cmis:objectId, cmis:name FROM $($qn.TypeQN) WHERE $($qn.PropQN) = $literal"
        try {
            $r = CmisPost $repoUrl @{
                cmisaction = 'query'; statement = $stmt; maxItems = 5; skipCount = 0
            }
        } catch {
            Warn "$label query ($mode) failed: $($_.Exception.Message)"
            continue
        }
        $rows = @(MemberValue $r 'results')
        if ($rows.Count -eq 0) { continue }
        if ($rows.Count -gt 1) {
            Warn "${label}: multiple folders for CIF $cif (using first; 1:1 assumption)."
        }
        $id   = PropValue $rows[0] 'cmis:objectId'
        $name = PropValue $rows[0] 'cmis:name'
        Ok ("$label root folder: '{0}'  objectId={1}  (cif literal: {2})" -f $name, $id, $mode)
        return @{ ObjectId = $id; Name = $name }
    }
    return $null
}

# ====================================================================
# Step 2: Resolve queryNames for both types
# ====================================================================
Section "Type definitions"
try {
    $src = ResolveQueryNames $SourceTypeId
    Ok ("Source type queryName  = {0}   (CIF property queryName = {1})" -f $src.TypeQN, $src.PropQN)
} catch { Fail $_.Exception.Message; exit 1 }
try {
    $tgt = ResolveQueryNames $TargetTypeId
    Ok ("Target type queryName  = {0}   (CIF property queryName = {1})" -f $tgt.TypeQN, $tgt.PropQN)
} catch { Fail $_.Exception.Message; exit 1 }

# ====================================================================
# Step 3: Resolve root folders for the CIF
# ====================================================================
Section "Root folders for CIF $Cif"
$srcRoot = FindRootFolder $src $Cif "SOURCE"
$tgtRoot = FindRootFolder $tgt $Cif "TARGET"
if (-not $srcRoot) { Fail "Source folder not found for CIF $Cif. Aborting."; exit 1 }
if (-not $tgtRoot) { Fail "Target folder not found for CIF $Cif. Aborting."; exit 1 }

# ====================================================================
# Tree walking
# ====================================================================
$script:topLevelSrcNames = New-Object System.Collections.Generic.List[string]
$script:topLevelTgtNames = New-Object System.Collections.Generic.List[string]

function FolderChildren([string]$folderId) {
    $escId = $folderId.Replace("'", "''")
    $stmt = "SELECT cmis:objectId, cmis:name FROM cmis:folder WHERE IN_FOLDER('$escId')"
    $r = CmisPost $repoUrl @{
        cmisaction = 'query'; statement = $stmt; maxItems = 500; skipCount = 0
    }
    return @(MemberValue $r 'results')
}

function FolderDocuments([string]$folderId, [int]$maxItems = 500) {
    $escId = $folderId.Replace("'", "''")
    $stmt = "SELECT cmis:objectId, cmis:name FROM cmis:document WHERE IN_FOLDER('$escId')"
    $r = CmisPost $repoUrl @{
        cmisaction = 'query'; statement = $stmt; maxItems = $maxItems; skipCount = 0
    }
    $rows = @(MemberValue $r 'results')
    $num  = MemberValue $r 'numItems'
    if ($null -eq $num) { $num = $rows.Count }
    return @{ Docs = $rows; NumItems = $num }
}

function ShowTree([string]$folderId, [string]$folderName, [int]$depth, [string]$prefix, [System.Collections.Generic.List[string]]$collectTopLevelInto) {
    if ($depth -gt $MaxDepth) {
        Write-Host "$prefix... (max depth reached, raise -MaxDepth to dig deeper)" -ForegroundColor DarkGray
        return
    }

    $docs = FolderDocuments $folderId 500
    $children = FolderChildren $folderId

    $childCount = $children.Count
    $docCount   = $docs.NumItems

    if ($depth -eq 0) {
        Write-Host ("{0}{1}/   [{2} subfolders, {3} docs]" -f $prefix, $folderName, $childCount, $docCount) -ForegroundColor White
    }

    if ($ShowDocs -and $docs.Docs.Count -gt 0) {
        foreach ($d in $docs.Docs) {
            $dn = PropValue $d 'cmis:name'
            Write-Host ("{0}  [D] {1}" -f $prefix, $dn) -ForegroundColor Gray
        }
    }

    foreach ($c in $children) {
        $cid   = PropValue $c 'cmis:objectId'
        $cname = PropValue $c 'cmis:name'
        if ($null -ne $collectTopLevelInto -and $depth -eq 0) {
            [void]$collectTopLevelInto.Add([string]$cname)
        }
        $cDocs = FolderDocuments $cid 1
        $cCounters = "[{0} docs]" -f $cDocs.NumItems
        Write-Host ("{0}  [F] {1}/   {2}" -f $prefix, $cname, $cCounters) -ForegroundColor Cyan
        ShowTree $cid $cname ($depth + 1) ($prefix + '    ') $null
    }
}

# ====================================================================
# Step 4: Print source tree
# ====================================================================
Section ("SOURCE TREE  (CIF {0})" -f $Cif)
ShowTree $srcRoot.ObjectId $srcRoot.Name 0 "" $script:topLevelSrcNames

# ====================================================================
# Step 5: Print target tree
# ====================================================================
Section ("TARGET TREE  (CIF {0})" -f $Cif)
ShowTree $tgtRoot.ObjectId $tgtRoot.Name 0 "" $script:topLevelTgtNames

# ====================================================================
# Step 6: Compare top-level subfolder names + recommend strategy
# ====================================================================
Section "Top-level subfolder comparison"

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
Section "Strategy recommendation"

if ($srcSet.Count -eq 0) {
    Info "Source has no subfolders. Use strategy A or C (whichever fits)."
    Info "Direct documents under source root can be moved with the current TUI flow if any exist."
}
elseif ($tgtSet.Count -eq 0) {
    Info "Target is empty (no subfolders)."
    Info "Recommended: A  (move source's direct children into target; subtree comes along)."
}
elseif ($onlySrc.Count -eq 0 -and $inBoth.Count -gt 0) {
    Info "All source subfolders also exist in target."
    Info "Recommended: B  (merge by name: move docs of each source subfolder into the same-named target subfolder)."
}
elseif ($inBoth.Count -gt 0 -and $onlySrc.Count -gt 0) {
    Info "Partial overlap. Some source subfolders exist in target, others do not."
    Info "Recommended: B  (merge by name; create the missing subfolders in target, or move them as units)."
}
else {
    Info "No overlap between source and target subfolder names."
    Info "Recommended: A  (move source's children into target as-is; you'll end up with both sets of subfolders side by side)."
}

Write-Host ""
Info "Reminder: strategies"
Info "  A = move source's direct children into target (preserves subtrees, may duplicate same-named folders)"
Info "  B = merge by name: align same-named subfolders, move docs into them, create the missing ones"
Info "  C = flatten: IN_TREE(source) WHERE cmis:document, move every descendant doc into target root"
