import { describe, expect, it } from 'vitest';
import { normalizeComposerModelId, parseComposerModelId, formatModelDisplayLabel, resolveAutoAvailableModel } from './modelSelection';
import type { AvailableModel, DefaultModelConfig } from '@/types/admin';

const am = (provider: string, model: string, kind = 'chat'): AvailableModel =>
  ({ provider_name: provider, model_name: model, kind, label: model } as unknown as AvailableModel);

describe('modelSelection', () => {
  it('normalizeComposerModelId returns auto for empty/auto', () => {
    expect(normalizeComposerModelId('', undefined)).toBe('auto');
    expect(normalizeComposerModelId('auto', undefined)).toBe('auto');
  });

  it('normalizeComposerModelId keeps provider/model format', () => {
    expect(normalizeComposerModelId('ds/model', [])).toBe('ds/model');
  });

  it('normalizeComposerModelId resolves legacy name via availableModels', () => {
    expect(normalizeComposerModelId('v4', [am('deepseek', 'v4')])).toBe('deepseek/v4');
  });

  it('parseComposerModelId splits on slash', () => {
    expect(parseComposerModelId('ds/m')).toEqual({ provider: 'ds', model: 'm' });
    expect(parseComposerModelId('noslash')).toBeNull();
    expect(parseComposerModelId('/leading')).toBeNull();
  });

  it('formatModelDisplayLabel', () => {
    expect(formatModelDisplayLabel('auto', 'Auto')).toBe('Auto');
    expect(formatModelDisplayLabel('x', 'V4', 'DeepSeek')).toBe('DeepSeek · V4');
    expect(formatModelDisplayLabel('x', 'V4')).toBe('V4');
  });

  it('resolveAutoAvailableModel picks default provider chat model', () => {
    const models = [am('ds', 'flash'), am('ds', 'pro'), am('qw', 'max')];
    const def: DefaultModelConfig = { provider: 'ds', model: 'pro' } as DefaultModelConfig;
    expect(resolveAutoAvailableModel(models, def)?.model_name).toBe('pro');
  });

  it('resolveAutoAvailableModel falls back to first provider', () => {
    const models = [am('ds', 'flash'), am('ds', 'pro')];
    expect(resolveAutoAvailableModel(models, undefined)?.model_name).toBe('flash');
  });

  it('resolveAutoAvailableModel returns undefined for empty', () => {
    expect(resolveAutoAvailableModel([], undefined)).toBeUndefined();
    expect(resolveAutoAvailableModel(undefined, undefined)).toBeUndefined();
  });
});
