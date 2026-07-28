import { describe, expect, it } from 'vitest';
import { parseGitHubSkillsRepoUrl } from './parseGitHubRepoUrl';

describe('parseGitHubSkillsRepoUrl', () => {
  it('parses basic owner/repo', () => {
    const r = parseGitHubSkillsRepoUrl('https://github.com/acme/skills');
    expect(r).toEqual({ owner: 'acme', repo: 'skills', ref: 'main', skillsPath: 'skills' });
  });

  it('parses tree URL with ref and path', () => {
    const r = parseGitHubSkillsRepoUrl('https://github.com/acme/myrepo/tree/dev/custom/skills');
    expect(r).toEqual({ owner: 'acme', repo: 'myrepo', ref: 'dev', skillsPath: 'custom/skills' });
  });

  it('strips .git suffix', () => {
    expect(parseGitHubSkillsRepoUrl('https://github.com/a/b.git')?.repo).toBe('b');
  });

  it('returns null for non-github hosts', () => {
    expect(parseGitHubSkillsRepoUrl('https://gitlab.com/a/b')).toBeNull();
  });

  it('returns null for empty or single-segment paths', () => {
    expect(parseGitHubSkillsRepoUrl('')).toBeNull();
    expect(parseGitHubSkillsRepoUrl('https://github.com/onlyone')).toBeNull();
  });

  it('handles URL without protocol', () => {
    const r = parseGitHubSkillsRepoUrl('github.com/owner/repo');
    expect(r?.owner).toBe('owner');
  });
});
