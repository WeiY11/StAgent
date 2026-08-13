import { describe, expect, it } from 'vitest';
import {
  desktopBuildVersionEnv,
  desktopSkillsInteropDiscoveryEnv,
} from './backend-env.js';

describe('desktopSkillsInteropDiscoveryEnv', () => {
  it('disables host-agent skill scanning by default', () => {
    expect(desktopSkillsInteropDiscoveryEnv({})).toBe('0');
    expect(desktopSkillsInteropDiscoveryEnv({ LEAGENT_SKILLS_INTEROP_DISCOVERY: ' ' })).toBe('0');
  });

  it('preserves an explicit user opt-in or opt-out', () => {
    expect(desktopSkillsInteropDiscoveryEnv({ LEAGENT_SKILLS_INTEROP_DISCOVERY: '1' })).toBe('1');
    expect(desktopSkillsInteropDiscoveryEnv({ LEAGENT_SKILLS_INTEROP_DISCOVERY: 'false' })).toBe('false');
  });
});

describe('desktopBuildVersionEnv', () => {
  it('forwards the packaged Electron version to the backend', () => {
    expect(desktopBuildVersionEnv('1.2.11')).toBe('1.2.11');
  });

  it('does not emit a blank runtime version', () => {
    expect(desktopBuildVersionEnv('   ')).toBe('unknown');
  });
});
