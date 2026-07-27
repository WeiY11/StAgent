import type hljs from 'highlight.js/lib/common';

type HighlightJsApi = typeof hljs;

let highlightJsPromise: Promise<HighlightJsApi> | null = null;
let highlightThemePromise: Promise<unknown> | null = null;

export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function loadHighlightTheme(): Promise<unknown> {
  highlightThemePromise ??= import('highlight.js/styles/github-dark.css');
  return highlightThemePromise;
}

function loadHighlightJs(): Promise<HighlightJsApi> {
  highlightJsPromise ??= Promise.all([
    import('highlight.js/lib/common'),
    loadHighlightTheme(),
  ]).then(([mod]) => mod.default);
  return highlightJsPromise;
}

export async function highlightCode(
  code: string,
  language = 'plaintext',
): Promise<string> {
  const safe = code.length > 0 ? code : ' ';
  try {
    const hljs = await loadHighlightJs();
    if (language && hljs.getLanguage(language)) {
      return hljs.highlight(safe, { language }).value;
    }
    return hljs.highlightAuto(safe).value;
  } catch {
    return escapeHtml(safe);
  }
}
