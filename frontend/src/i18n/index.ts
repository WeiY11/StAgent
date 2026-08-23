import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

export type SupportedLng = 'zh-CN' | 'en-US';

const COOKIE_NAME = 'StAgent-lang';
const STORAGE_KEY = 'StAgent-language';

/**
 * All translation bundle file names.
 * Each file is loaded as its own i18next namespace AND merged into the shared
 * `translation` namespace so that legacy `t('chat.xxx')` calls keep working.
 */
export const TRANSLATION_BUNDLE_FILES = [
  'common',
  'nav',
  'auth',
  'dashboard',
  'workflows',
  'knowledge',
  'integrations',
  'chat',
  'settings',
  'admin',
  'modals',
  'errors',
  'docs',
  'accounts',
  'notifications',
  'pet',
  'about',
  'codingProjects',
  'complaints',
  'missions',
] as const;

export type TranslationBundleName = (typeof TRANSLATION_BUNDLE_FILES)[number];

/**
 * Core namespaces that are eagerly loaded at init.
 * They cover the most common UX paths (navigation, auth, shared widgets, errors).
 */
export const CORE_BUNDLES: ReadonlySet<TranslationBundleName> = new Set([
  'common',
  'nav',
  'auth',
  'errors',
]);

/**
 * Non-core namespaces that are lazy-loaded after the initial render.
 */
export const NON_CORE_BUNDLES: TranslationBundleName[] =
  TRANSLATION_BUNDLE_FILES.filter((b) => !CORE_BUNDLES.has(b));

/** All bundle names as a plain array (used for i18next `ns` config). */
const ALL_NS: string[] = [...TRANSLATION_BUNDLE_FILES];

/** Languages that have been fully loaded (all core + non-core bundles). */
const loadedLanguages = new Set<string>();

/** Tracks bundles that have been loaded to avoid duplicate imports. */
const loadedBundleKeys = new Set<string>();

// ─── helpers ────────────────────────────────────────────────────────────────

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${name.replace(/([.$?*|{}()[\]\\/+^])/g, '\\$1')}=([^;]*)`),
  );
  return match?.[1] ? decodeURIComponent(match[1]) : null;
}

function normalizeLanguageCode(raw: string | undefined | null): SupportedLng {
  if (!raw) return 'zh-CN';
  const lower = raw.toLowerCase();
  if (lower === 'en' || lower.startsWith('en-')) return 'en-US';
  if (lower.startsWith('zh')) return 'zh-CN';
  return 'zh-CN';
}

/** Migrate legacy storage values before detection runs. */
function migrateLegacyLanguage(): void {
  if (typeof localStorage === 'undefined') return;
  const ls = localStorage.getItem(STORAGE_KEY);
  if (ls === 'en') localStorage.setItem(STORAGE_KEY, 'en-US');

  const c = readCookie(COOKIE_NAME);
  if (c === 'en') {
    persistLanguageChoice('en-US');
  }
}

/**
 * Detection order (aligned with i18next-browser-languagedetector):
 * cookie → localStorage → navigator → html lang.
 */
export function resolveInitialLanguage(): SupportedLng {
  migrateLegacyLanguage();
  const fromCookie = readCookie(COOKIE_NAME);
  const fromLs =
    typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null;
  const raw = fromCookie || fromLs;
  if (raw === 'zh-CN' || raw === 'en-US') return raw;
  if (raw === 'en') return 'en-US';

  if (typeof navigator !== 'undefined') {
    const nav = navigator.language || navigator.languages?.[0];
    return normalizeLanguageCode(nav);
  }

  return 'zh-CN';
}

export function persistLanguageChoice(lng: string): void {
  const normalized = lng === 'en' ? 'en-US' : lng;
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(STORAGE_KEY, normalized);
  }
  if (typeof document !== 'undefined') {
    document.cookie = `${COOKIE_NAME}=${encodeURIComponent(normalized)}; Path=/; Max-Age=31536000; SameSite=Lax`;
    document.documentElement.lang = normalized;
  }
}

function syncDocumentMeta(): void {
  if (typeof document === 'undefined') return;
  const title = i18n.t('common.meta.appTitle');
  const desc = i18n.t('common.meta.appDescription');
  document.title = title;
  const meta = document.querySelector('meta[name="description"]');
  if (meta) meta.setAttribute('content', desc);
}

// ─── bundle loading ─────────────────────────────────────────────────────────

/**
 * Import bundle JSON files and return a map of { name → data }.
 * Does NOT touch i18next — caller decides how to register the resources.
 */
async function importBundles(
  lng: SupportedLng,
  bundles: readonly TranslationBundleName[],
): Promise<Map<TranslationBundleName, Record<string, unknown>>> {
  const results = new Map<TranslationBundleName, Record<string, unknown>>();
  await Promise.all(
    bundles.map(async (name) => {
      const mod = await import(`./locales/${lng}/${name}.json`);
      results.set(name, (mod.default ?? mod) as Record<string, unknown>);
    }),
  );
  return results;
}

/**
 * Register loaded bundle data into i18next: each file as its own namespace
 * AND merged into the shared `translation` namespace.
 */
function registerBundles(
  lng: SupportedLng,
  bundles: Map<TranslationBundleName, Record<string, unknown>>,
): void {
  for (const [name, data] of bundles) {
    const key = `${lng}:${name}`;
    if (loadedBundleKeys.has(key)) continue;
    loadedBundleKeys.add(key);
    // Register as own namespace
    i18n.addResourceBundle(lng, name, data, true, true);
    // Also merge into the shared `translation` namespace
    i18n.addResourceBundle(lng, 'translation', data, true, true);
  }
}

/**
 * Dynamically import a single bundle file and register it in both:
 *  - its own namespace (e.g. `chat`)
 *  - the merged `translation` namespace (for backward-compatible `t('chat.xxx')` calls)
 */
export async function loadTranslationBundle(
  lng: SupportedLng,
  name: TranslationBundleName,
): Promise<void> {
  const key = `${lng}:${name}`;
  if (loadedBundleKeys.has(key)) return;
  const bundles = await importBundles(lng, [name]);
  registerBundles(lng, bundles);
}

// ─── public API ─────────────────────────────────────────────────────────────

/**
 * Ensures **all** bundles (core + non-core) for `lng` are loaded (idempotent).
 */
export async function ensureTranslationForLanguage(lng: SupportedLng): Promise<void> {
  if (loadedLanguages.has(lng)) return;
  const bundles = await importBundles(lng, TRANSLATION_BUNDLE_FILES);
  registerBundles(lng, bundles);
  loadedLanguages.add(lng);
}

/**
 * Loads resources for `lng` if needed, then switches language
 * (use from Settings / Header instead of bare `changeLanguage`).
 */
export async function changeAppLanguage(rawLng: string): Promise<void> {
  const lng = normalizeLanguageCode(rawLng) as SupportedLng;
  await ensureTranslationForLanguage(lng);
  await i18n.changeLanguage(lng);
}

/** @deprecated Use changeAppLanguage */
export async function ensureLocaleLoaded(lng: string): Promise<void> {
  await ensureTranslationForLanguage(normalizeLanguageCode(lng) as SupportedLng);
}

/**
 * Lazy-load a single namespace on demand.
 * Useful for route-level code-splitting:
 * ```ts
 * await loadNamespace('admin');
 * ```
 */
export async function loadNamespace(ns: TranslationBundleName): Promise<void> {
  const lng = i18n.language as SupportedLng;
  await loadTranslationBundle(lng, ns);
}

// ─── init ───────────────────────────────────────────────────────────────────

let initPromise: Promise<void> | null = null;

/** Idempotent — safe for main entry and Vitest (same module instance). */
export function initI18n(): Promise<void> {
  if (initPromise) return initPromise;
  initPromise = (async () => {
    const initialLng = resolveInitialLanguage();

    // 1. Eagerly load core bundles (common, nav, auth, errors) first.
    const coreBundles = await importBundles(initialLng, [
      ...CORE_BUNDLES,
    ] as TranslationBundleName[]);

    // Build the initial merged `translation` resource from core bundles.
    const coreTranslation: Record<string, unknown> = {};
    for (const data of coreBundles.values()) {
      Object.assign(coreTranslation, data);
    }

    await i18n.use(initReactI18next).init({
      lng: initialLng,
      fallbackLng: 'zh-CN',
      supportedLngs: ['zh-CN', 'en-US'],
      load: 'currentOnly',

      // Every bundle file is its own namespace; `translation` holds the merged
      // content for backward-compatible cross-namespace `t('chat.xxx')` calls.
      ns: [...ALL_NS, 'translation'],
      defaultNS: 'translation',

      resources: {
        [initialLng]: {
          translation: coreTranslation,
          // Register core bundles as their own namespaces too.
          ...Object.fromEntries(coreBundles),
        },
      },

      interpolation: {
        escapeValue: false,
      },
      react: {
        useSuspense: false,
      },
      debug: import.meta.env.DEV,
    });

    // Mark core bundles as loaded.
    for (const name of CORE_BUNDLES) {
      loadedBundleKeys.add(`${initialLng}:${name}`);
    }

    syncDocumentMeta();

    i18n.on('languageChanged', () => {
      persistLanguageChoice(i18n.language);
      syncDocumentMeta();
    });

    persistLanguageChoice(i18n.language);

    // 2. Lazy-load non-core bundles after init (each is a separate Vite chunk).
    //    They are registered as their own namespaces AND merged into `translation`.
    const nonCoreBundles = await importBundles(initialLng, NON_CORE_BUNDLES);
    registerBundles(initialLng, nonCoreBundles);

    loadedLanguages.add(initialLng);
  })();

  return initPromise;
}

/** Re-export so `i18next-browser-languagedetector` stays linked to the same detection contract as `resolveInitialLanguage`. */
export { default as browserLanguageDetector } from 'i18next-browser-languagedetector';

export default i18n;
