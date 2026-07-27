/**
 * SettingsPage — shared types, constants, and helpers.
 * Extracted from the monolithic index.tsx to improve readability and
 * enable tree-shaking of non-rendered code.
 */
// eslint-disable-next-line import-x/order -- Existing file header encoding trips the sorter here.
import {
  Globe,
  FolderOpen,
  Bot,
  Zap,
  Database,
  Bell,
  Code,
  Sun,
  Moon,
  Monitor,
  Clock,
  Sparkles,
  Cpu,
  Flame,
  Star,
  Minimize2,
} from 'lucide-react';
/* ── Types ── */
type LogoBackdropPreset = 'auto' | 'aurora' | 'circuit' | 'ember' | 'void' | 'minimal';
type BrandFontPreset = 'modern' | 'rounded' | 'handwritten' | 'mono';

export type SettingsTab =
  | 'general'
  | 'tools'
  | 'schedule'
  | 'models'
  | 'plugins'
  | 'tokens'
  | 'mail'
  | 'pythonPackages'
  | 'about';

export interface ExtensionPackRow {
  id: string;
  name: string;
  description: string;
  installed: boolean;
}

export interface PythonPackageRow {
  name: string;
  version: string;
  is_direct?: boolean;
  latest_version?: string;
}

export interface PythonEnvInfoResponse {
  python_executable: string;
  backend_root: string;
  uses_uv: boolean;
  project_mode: boolean;
  has_lockfile: boolean;
}

export type PythonFilterMode = 'all' | 'direct' | 'outdated';

/** GET /settings/mail */
export interface MailStatusApiResponse {
  host: string;
  port: number;
  from_email: string;
  from_name: string;
  use_tls: boolean;
  use_ssl: boolean;
  username_set: boolean;
  password_set: boolean;
}

export type MailEncryptionMode = 'starttls' | 'ssl' | 'none';

export function mailEncryptionFromFlags(use_tls: boolean, use_ssl: boolean): MailEncryptionMode {
  if (use_ssl) return 'ssl';
  if (use_tls) return 'starttls';
  return 'none';
}

/** Backend GET /settings/tokens entry */
export interface TokenStatusApiEntry {
  env_key: string;
  set: boolean;
}

export interface Tool {
  id: string;
  nameKey: string;
  descriptionKey: string;
  category: string;
  enabled: boolean;
}

/* ── Constants ── */

export const TOKEN_ENV_ROWS: { key: string; labelKey: string }[] = [
  { key: 'LEAGENT_GITHUB_TOKEN', labelKey: 'settings.tokensGithubWa' },
  { key: 'GITHUB_TOKEN', labelKey: 'settings.tokensGithub' },
  { key: 'OPENAI_API_KEY', labelKey: 'settings.tokensOpenai' },
  { key: 'ANTHROPIC_API_KEY', labelKey: 'settings.tokensAnthropic' },
  { key: 'DEEPSEEK_API_KEY', labelKey: 'settings.tokensDeepseek' },
  { key: 'DEEPSEEK_BASE_URL', labelKey: 'settings.tokensDeepseekBaseUrl' },
  { key: 'DEEPSEEK_MODEL', labelKey: 'settings.tokensDeepseekModel' },
  { key: 'DEEPSEEK_THINKING_TYPE', labelKey: 'settings.tokensDeepseekThinkingType' },
  { key: 'DEEPSEEK_REASONING_EFFORT', labelKey: 'settings.tokensDeepseekReasoningEffort' },
  { key: 'DASHSCOPE_API_KEY', labelKey: 'settings.tokensDashscope' },
  { key: 'WEB_SEARCH_PROVIDER', labelKey: 'settings.tokensWebSearchProvider' },
  { key: 'WEB_SEARCH_BING_API_KEY', labelKey: 'settings.tokensWebSearchBing' },
  { key: 'WEB_SEARCH_SEARXNG_BASE_URL', labelKey: 'settings.tokensWebSearchSearxng' },
  { key: 'WEB_SEARCH_BRAVE_API_KEY', labelKey: 'settings.tokensWebSearchBrave' },
  { key: 'WEB_SEARCH_TAVILY_API_KEY', labelKey: 'settings.tokensWebSearchTavily' },
  { key: 'WEB_SEARCH_EXA_API_KEY', labelKey: 'settings.tokensWebSearchExa' },
  { key: 'WEB_SEARCH_FIRECRAWL_API_KEY', labelKey: 'settings.tokensWebSearchFirecrawl' },
  { key: 'WEB_SEARCH_FIRECRAWL_API_URL', labelKey: 'settings.tokensWebSearchFirecrawlUrl' },
  { key: 'WEB_SEARCH_SERPER_API_KEY', labelKey: 'settings.tokensWebSearchSerper' },
  { key: 'IMAGE_SEARCH_API_KEY', labelKey: 'settings.tokensImageSearchApiKey' },
  { key: 'IMAGE_SEARCH_CX', labelKey: 'settings.tokensImageSearchCx' },
  { key: 'WEB_FETCH_ENABLED', labelKey: 'settings.tokensWebFetchEnabled' },
  { key: 'WEB_FETCH_CHECK_ROBOTS', labelKey: 'settings.tokensWebFetchRobots' },
  { key: 'WEB_FETCH_MIN_INTERVAL_MS', labelKey: 'settings.tokensWebFetchInterval' },
  { key: 'WEB_FETCH_USER_AGENT', labelKey: 'settings.tokensWebFetchUa' },
  { key: 'WEB_FETCH_CACHE_TTL_MINUTES', labelKey: 'settings.tokensWebFetchCacheTtl' },
];

export const CATEGORY_ICONS: Record<string, typeof Globe> = {
  web: Globe,
  file: FolderOpen,
  llm: Bot,
  api: Zap,
  data: Database,
  notification: Bell,
  custom: Code,
};

export const MOCK_TOOLS: Tool[] = [
  { id: '1', nameKey: 'settings.mockTools.webSearch.name', descriptionKey: 'settings.mockTools.webSearch.description', category: 'web', enabled: true },
  { id: '2', nameKey: 'settings.mockTools.fileRead.name', descriptionKey: 'settings.mockTools.fileRead.description', category: 'file', enabled: true },
  { id: '3', nameKey: 'settings.mockTools.gpt4.name', descriptionKey: 'settings.mockTools.gpt4.description', category: 'llm', enabled: true },
  { id: '4', nameKey: 'settings.mockTools.claude.name', descriptionKey: 'settings.mockTools.claude.description', category: 'llm', enabled: false },
  { id: '5', nameKey: 'settings.mockTools.apiCall.name', descriptionKey: 'settings.mockTools.apiCall.description', category: 'api', enabled: true },
  { id: '6', nameKey: 'settings.mockTools.dbQuery.name', descriptionKey: 'settings.mockTools.dbQuery.description', category: 'data', enabled: false },
];

export const DEFAULT_BRAND = { bg: 'bg-gray-100 dark:bg-surface', text: 'text-gray-700 dark:text-gray-300', letter: '?' } as const;

export const PROVIDER_BRAND: Record<string, { bg: string; text: string; letter: string }> = {
  openai: { bg: 'bg-mint-100 dark:bg-mint-900/30', text: 'text-mint-700 dark:text-mint-400', letter: 'O' },
  anthropic: { bg: 'bg-peach-100 dark:bg-peach-900/30', text: 'text-peach-700 dark:text-peach-400', letter: 'A' },
  qwen: { bg: 'bg-sky-100 dark:bg-sky-900/30', text: 'text-sky-800 dark:text-sky-400', letter: 'Q' },
  dashscope: { bg: 'bg-sky-100 dark:bg-sky-900/30', text: 'text-sky-800 dark:text-sky-400', letter: 'D' },
  deepseek: { bg: 'bg-blue-100 dark:bg-blue-900/30', text: 'text-blue-700 dark:text-blue-400', letter: 'DS' },
  ollama: { bg: 'bg-gray-100 dark:bg-surface', text: 'text-gray-700 dark:text-gray-300', letter: 'L' },
  custom: { bg: 'bg-gray-100 dark:bg-surface', text: 'text-gray-700 dark:text-gray-300', letter: 'C' },
  azure: { bg: 'bg-sky-100 dark:bg-sky-900/30', text: 'text-sky-700 dark:text-sky-400', letter: 'Az' },
};

export const THEME_OPTIONS = [
  { value: 'light' as const, label: 'settings.themeLight', icon: Sun },
  { value: 'dark' as const, label: 'settings.themeDark', icon: Moon },
  { value: 'system' as const, label: 'settings.themeSystem', icon: Monitor },
] as const;

export const BRANDING_BACKDROP_META: Record<
  LogoBackdropPreset,
  { labelKey: string; icon: typeof Sun }
> = {
  auto: { labelKey: 'settings.brandingBackdropAuto', icon: Clock },
  aurora: { labelKey: 'settings.brandingBackdropAurora', icon: Sparkles },
  circuit: { labelKey: 'settings.brandingBackdropCircuit', icon: Cpu },
  ember: { labelKey: 'settings.brandingBackdropEmber', icon: Flame },
  void: { labelKey: 'settings.brandingBackdropVoid', icon: Star },
  minimal: { labelKey: 'settings.brandingBackdropMinimal', icon: Minimize2 },
};

export const BRAND_FONT_META: Record<
  BrandFontPreset,
  { labelKey: string; sample: string; className: string }
> = {
  modern: {
    labelKey: 'settings.brandFontModern',
    sample: 'LeAgent',
    className: 'font-sans tracking-[-0.035em]',
  },
  rounded: {
    labelKey: 'settings.brandFontRounded',
    sample: 'LeAgent',
    className: 'font-sans tracking-[-0.025em]',
  },
  handwritten: {
    labelKey: 'settings.brandFontHandwritten',
    sample: 'LeAgent',
    className: 'tracking-[-0.015em] [font-family:"Comic_Sans_MS","Bradley_Hand","Segoe_Print",cursive]',
  },
  mono: {
    labelKey: 'settings.brandFontMono',
    sample: 'LeAgent',
    className: 'font-mono tracking-[-0.04em]',
  },
};

/* ── Helpers ── */

import type { DeepSeekBalanceResponse } from '@/types/admin';

export type DeepSeekBalanceStatus =
  | { state: 'loading' }
  | { state: 'success'; data: DeepSeekBalanceResponse }
  | { state: 'error'; error: string };

export function formatDeepSeekBalance(data: DeepSeekBalanceResponse): string {
  const balances = data.balance_infos
    .map((item) => {
      const total = item.total_balance || item.topped_up_balance || item.granted_balance;
      return total ? `${total} ${item.currency}`.trim() : '';
    })
    .filter(Boolean);

  return balances.length > 0 ? balances.join(' · ') : '—';
}
