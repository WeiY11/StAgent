$lib = "d:\LeAgent\frontend\src\lib"

# Create subdirectories
$dirs = @("stream","pet","canvas","chat","agent","auth","workflow","knowledge","utils")
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Path "$lib\$d" -Force | Out-Null
}

# stream/
$stream = @("chatStreamBatching.ts","chatStreamEvents.ts","chatStreamEvents.assistantMedia.test.ts","jsonStreamField.ts","runChatStream.ts","runChatStream.checkpoint.test.ts","runChatStream.folder.test.ts","runChatStream.projectFolder.test.ts","streamPreviewLimits.ts")
foreach ($f in $stream) { git mv "$lib\$f" "$lib\stream\$f" }

# pet/
$pet = @("builtinPets.ts","petActionCatalog.ts","petAppearanceMime.ts","petBehaviorVisual.ts","petBehaviorVisual.test.ts","petPresetManifest.ts","petPresetSamples.ts","petSettings.ts","petSettings.test.ts","petShowcasePresets.ts","spriteSheetToGif.ts","spriteSheetToGif.test.ts")
foreach ($f in $pet) { git mv "$lib\$f" "$lib\pet\$f" }

# canvas/
$canvas = @("canvasPreviewDoc.ts","canvasPreviewDoc.test.ts","canvasPreviewJs.ts","canvasPreviewJs.test.ts","canvasScreenshot.ts","canvasScreenshot.test.ts","canvasStreamPreview.ts","canvasStreamPreview.test.ts","codeExecutionStreamPreview.ts","codeExecutionStreamPreview.test.ts","docProcessorStreamPreview.ts","docProcessorStreamPreview.test.ts","genUiActionBus.ts","genUiActionBus.test.ts","genUiUrl.ts","nestedAgentStreamPreview.ts","nestedAgentStreamPreview.test.ts")
foreach ($f in $canvas) { git mv "$lib\$f" "$lib\canvas\$f" }

# chat/
$chat = @("chatAttachments.ts","chatAttachments.test.ts","composerTriggerToken.ts","composerTriggerToken.test.ts","emptyChatMessages.ts","knowledgeChatToken.ts","parseUserMessageRefs.ts","parseUserMessageRefs.test.ts","regenerateAssistantReply.ts","sessionProjectEdits.ts","sessionProjectEdits.test.ts","skillChatToken.ts")
foreach ($f in $chat) { git mv "$lib\$f" "$lib\chat\$f" }

# agent/
$agent = @("agentImageArtifacts.ts","agentImageArtifacts.test.ts","agentPathDisplay.ts","agentPathDisplay.test.ts","agentSessionEvents.ts","agentSessionEvents.test.ts","projectToolEnvelope.ts","projectToolEnvelope.test.ts","toolCallArgsDisplay.ts","toolCallArgsDisplay.test.ts")
foreach ($f in $agent) { git mv "$lib\$f" "$lib\agent\$f" }

# auth/
$auth = @("authUser.ts","authedTransport.ts","downloadAuthenticatedFile.ts","machineFingerprint.ts")
foreach ($f in $auth) { git mv "$lib\$f" "$lib\auth\$f" }

# workflow/
$workflow = @("chatSpecToFlowData.ts","chatSpecToFlowData.test.ts","parseFlowDataFromApi.ts","workflowLayout.ts","workflowLayout.test.ts")
foreach ($f in $workflow) { git mv "$lib\$f" "$lib\workflow\$f" }

# knowledge/
$knowledge = @("knowledgeSearch.ts","knowledgeSearch.test.ts","knowledgeSession.ts")
foreach ($f in $knowledge) { git mv "$lib\$f" "$lib\knowledge\$f" }

# utils/
$utils = @("brandingBackdrop.ts","brandingIcon.ts","cameraAccess.ts","cameraAccess.test.ts","categoryIcons.tsx","contextBudget.ts","contextBudget.test.ts","deepseekSetupNudge.ts","deepseekSetupNudge.test.ts","highlight.ts","lazyImportWithRetry.ts","localFolderPath.ts","localFolderPath.test.ts","mimeForPreview.ts","mimeForPreview.test.ts","modelSelection.ts","modelTasks.ts","parseCsvForPreview.ts","parseGitHubRepoUrl.ts","persistNamespace.ts","previewUrl.ts","previewUrl.test.ts","queryClient.ts","utils.ts","utils.test.ts")
foreach ($f in $utils) { git mv "$lib\$f" "$lib\utils\$f" }

Write-Host "Done moving files."
