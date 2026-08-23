$src = "d:\StAgent\frontend\src"
$lib = "$src\lib"

$map = @{
    "chatStreamBatching" = "stream"; "chatStreamEvents" = "stream"; "jsonStreamField" = "stream"
    "runChatStream" = "stream"; "streamPreviewLimits" = "stream"
    "builtinPets" = "pet"; "petActionCatalog" = "pet"; "petAppearanceMime" = "pet"
    "petBehaviorVisual" = "pet"; "petPresetManifest" = "pet"; "petPresetSamples" = "pet"
    "petSettings" = "pet"; "petShowcasePresets" = "pet"; "spriteSheetToGif" = "pet"
    "canvasPreviewDoc" = "canvas"; "canvasPreviewJs" = "canvas"; "canvasScreenshot" = "canvas"
    "canvasStreamPreview" = "canvas"; "codeExecutionStreamPreview" = "canvas"
    "docProcessorStreamPreview" = "canvas"; "genUiActionBus" = "canvas"
    "genUiUrl" = "canvas"; "nestedAgentStreamPreview" = "canvas"
    "chatAttachments" = "chat"; "composerTriggerToken" = "chat"; "emptyChatMessages" = "chat"
    "knowledgeChatToken" = "chat"; "parseUserMessageRefs" = "chat"
    "regenerateAssistantReply" = "chat"; "sessionProjectEdits" = "chat"; "skillChatToken" = "chat"
    "agentImageArtifacts" = "agent"; "agentPathDisplay" = "agent"; "agentSessionEvents" = "agent"
    "projectToolEnvelope" = "agent"; "toolCallArgsDisplay" = "agent"
    "authUser" = "auth"; "authedTransport" = "auth"
    "downloadAuthenticatedFile" = "auth"; "machineFingerprint" = "auth"
    "chatSpecToFlowData" = "workflow"; "parseFlowDataFromApi" = "workflow"; "workflowLayout" = "workflow"
    "knowledgeSearch" = "knowledge"; "knowledgeSession" = "knowledge"
    "brandingBackdrop" = "utils"; "brandingIcon" = "utils"; "cameraAccess" = "utils"
    "categoryIcons" = "utils"; "contextBudget" = "utils"; "deepseekSetupNudge" = "utils"
    "highlight" = "utils"; "lazyImportWithRetry" = "utils"; "localFolderPath" = "utils"
    "mimeForPreview" = "utils"; "modelSelection" = "utils"; "modelTasks" = "utils"
    "parseCsvForPreview" = "utils"; "parseGitHubRepoUrl" = "utils"; "persistNamespace" = "utils"
    "previewUrl" = "utils"; "queryClient" = "utils"; "utils" = "utils"
}

# Lookahead: module name must end at quote, semicolon, whitespace, comma, or paren (NOT slash)
$la = '(?=[''"\s;,)])(?!\/)'
$laSimple = '(?=[''"\s])'

$files = Get-ChildItem -Path $src -Recurse -Include *.ts,*.tsx
$count = 0

foreach ($file in $files) {
    $content = Get-Content $file.FullName -Raw -Encoding UTF8
    if ($null -eq $content) { continue }
    $original = $content

    $relPath = $file.FullName.Substring($lib.Length + 1)
    $parts = $relPath -split '\\'
    $inLibSubdir = $parts.Length -gt 1
    $fileSubdir = if ($inLibSubdir) { $parts[0] } else { $null }

    foreach ($mod in $map.Keys) {
        $subdir = $map[$mod]

        # Pattern 1: @/lib/moduleName -> @/lib/subdir/moduleName
        $p1 = '(@/lib/)(' + $mod + ')' + $la
        $r1 = '${1}' + $subdir + '/${2}'
        $content = [regex]::Replace($content, $p1, $r1)

        # Pattern 2: relative ./moduleName in lib/ files (cross-subdir only)
        if ($inLibSubdir -and ($fileSubdir -ne $subdir)) {
            $p2 = '(from [' + "'" + '"' + '])\.\/(' + $mod + ')(?=[' + "'" + '"' + '\s])'
            $r2 = '${1}./' + $subdir + '/${2}'
            $content = [regex]::Replace($content, $p2, $r2)
        }

        # Pattern 3: ../lib/moduleName from lib/ subdirs
        $p3 = '(\.\.\/lib\/)(' + $mod + ')' + $la
        $r3 = '${1}' + $subdir + '/${2}'
        $content = [regex]::Replace($content, $p3, $r3)
    }

    if ($content -ne $original) {
        Set-Content -Path $file.FullName -Value $content -Encoding UTF8 -NoNewline
        $count++
    }
}

Write-Host "Updated imports in $count files."
