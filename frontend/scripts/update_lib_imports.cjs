const fs = require('fs');
const path = require('path');

const srcDir = path.join(__dirname, '..', 'src');
const libDir = path.join(srcDir, 'lib');

const modToSubdir = {
  // stream/
  chatStreamBatching: 'stream', chatStreamEvents: 'stream', jsonStreamField: 'stream',
  runChatStream: 'stream', streamPreviewLimits: 'stream',
  // pet/
  builtinPets: 'pet', petActionCatalog: 'pet', petAppearanceMime: 'pet',
  petBehaviorVisual: 'pet', petPresetManifest: 'pet', petPresetSamples: 'pet',
  petSettings: 'pet', petShowcasePresets: 'pet', spriteSheetToGif: 'pet',
  // canvas/
  canvasPreviewDoc: 'canvas', canvasPreviewJs: 'canvas', canvasScreenshot: 'canvas',
  canvasStreamPreview: 'canvas', codeExecutionStreamPreview: 'canvas',
  docProcessorStreamPreview: 'canvas', genUiActionBus: 'canvas',
  genUiUrl: 'canvas', nestedAgentStreamPreview: 'canvas',
  // chat/
  chatAttachments: 'chat', composerTriggerToken: 'chat', emptyChatMessages: 'chat',
  knowledgeChatToken: 'chat', parseUserMessageRefs: 'chat',
  regenerateAssistantReply: 'chat', sessionProjectEdits: 'chat', skillChatToken: 'chat',
  // agent/
  agentImageArtifacts: 'agent', agentPathDisplay: 'agent', agentSessionEvents: 'agent',
  projectToolEnvelope: 'agent', toolCallArgsDisplay: 'agent',
  // auth/
  authUser: 'auth', authedTransport: 'auth',
  downloadAuthenticatedFile: 'auth', machineFingerprint: 'auth',
  // workflow/
  chatSpecToFlowData: 'workflow', parseFlowDataFromApi: 'workflow', workflowLayout: 'workflow',
  // knowledge/
  knowledgeSearch: 'knowledge', knowledgeSession: 'knowledge',
  // utils/
  brandingBackdrop: 'utils', brandingIcon: 'utils', cameraAccess: 'utils',
  categoryIcons: 'utils', contextBudget: 'utils', deepseekSetupNudge: 'utils',
  highlight: 'utils', lazyImportWithRetry: 'utils', localFolderPath: 'utils',
  mimeForPreview: 'utils', modelSelection: 'utils', modelTasks: 'utils',
  parseCsvForPreview: 'utils', parseGitHubRepoUrl: 'utils', persistNamespace: 'utils',
  previewUrl: 'utils', queryClient: 'utils', utils: 'utils',
};

function getAllFiles(dir, ext) {
  let results = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      results = results.concat(getAllFiles(full, ext));
    } else if (ext.some(e => entry.name.endsWith(e))) {
      results.push(full);
    }
  }
  return results;
}

// Build regex patterns once
// Match @/lib/moduleName where moduleName is NOT followed by / (to avoid double-replace)
const entries = Object.entries(modToSubdir);
let count = 0;

const allFiles = getAllFiles(srcDir, ['.ts', '.tsx']);

for (const filePath of allFiles) {
  let content = fs.readFileSync(filePath, 'utf8');
  const original = content;

  // Determine if this file is inside a lib/ subdirectory
  const relToLib = path.relative(libDir, filePath);
  const parts = relToLib.split(path.sep);
  const inLibSubdir = parts.length > 1;
  const fileSubdir = inLibSubdir ? parts[0] : null;

  for (const [mod, subdir] of entries) {
    const safeMod = mod.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    // Pattern 1: @/lib/mod followed by quote/semicolon/etc (not slash)
    const re1 = new RegExp(`(@/lib/)(${safeMod})(?=['"\\s;,)])`, 'g');
    content = content.replace(re1, `$1${subdir}/$2`);

    // Pattern 2: relative ./moduleName in lib/ files (cross-subdir only)
    if (inLibSubdir && fileSubdir !== subdir) {
      const re2 = new RegExp(`(from ['"])\\.\/(${safeMod})(?=['"\\s])`, 'g');
      content = content.replace(re2, `$1./${subdir}/$2`);
    }

    // Pattern 3: ../lib/moduleName from lib/ subdirs
    const re3 = new RegExp(`(\\.\\.\\/lib\\/)(${safeMod})(?=['"\\s;,)])`, 'g');
    content = content.replace(re3, `$1${subdir}/$2`);
  }

  if (content !== original) {
    fs.writeFileSync(filePath, content, 'utf8');
    count++;
  }
}

console.log(`Updated imports in ${count} files.`);
