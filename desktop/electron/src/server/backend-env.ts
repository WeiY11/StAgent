/** Desktop defaults for backend process environment variables. */

/**
 * Skip cross-agent skill discovery by default in the desktop app.
 *
 * Desktop startup should not depend on the size or validity of unrelated
 * Codex, Claude, or Cursor skill directories on the host. Users who want
 * that interoperability can opt in with LEAGENT_SKILLS_INTEROP_DISCOVERY=1.
 */
export function desktopSkillsInteropDiscoveryEnv(env: NodeJS.ProcessEnv): string {
  return env.LEAGENT_SKILLS_INTEROP_DISCOVERY?.trim() || '0';
}

/** Return a non-empty build version for the backend health and metadata APIs. */
export function desktopBuildVersionEnv(appVersion: string): string {
  return appVersion.trim() || 'unknown';
}
