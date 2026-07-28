import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui';
import { Textarea } from '@/components/ui/Textarea';
import { useToast } from '@/components/ui/Toaster';
import { cn } from '@/lib/utils/utils';
import { petSpaceApi, type PetProject } from '@/api/petSpace';
import {
  PET_PERSONALITY_MAX_CHARS,
  resolvedPersonalityDocument,
  type PetSettings,
  type PetSettingsClipPatch,
} from '@/lib/pet/petSettings';

export interface PersonalityEditorProps {
  project: PetProject;
  primaryParsed: PetSettings;
  patchSettingsMut: {
    isPending: boolean;
    mutateAsync: (vars: {
      projectId: string;
      baseSettings: string | null | undefined;
      patch: Partial<Omit<PetSettings, 'clips'>> & { clips?: PetSettingsClipPatch };
    }) => Promise<unknown>;
  };
}

export function PersonalityEditor({ project, primaryParsed, patchSettingsMut }: PersonalityEditorProps) {
  const { t } = useTranslation();
  const { toast } = useToast();
  const stored = resolvedPersonalityDocument(primaryParsed);
  const defaultPersonalityQuery = useQuery({
    queryKey: ['pet-space', 'personality', 'default'],
    queryFn: () => petSpaceApi.getDefaultPersonality(),
    staleTime: 60 * 60 * 1000,
  });
  const defaultDocument = defaultPersonalityQuery.data?.document.trim() ?? '';
  const [draft, setDraft] = useState<string>(stored || defaultDocument);

  useEffect(() => {
    setDraft(stored || defaultDocument);
  }, [defaultDocument, stored, project.id]);

  const trimmed = draft.trim();
  const charCount = draft.length;
  const overLimit = charCount > PET_PERSONALITY_MAX_CHARS;
  const isDirty = draft !== stored;
  const canSave = isDirty && !overLimit && !patchSettingsMut.isPending;

  const handleSave = useCallback(async () => {
    try {
      await patchSettingsMut.mutateAsync({
        projectId: project.id,
        baseSettings: project.settings,
        patch: {
          personality: trimmed ? { document: trimmed } : null,
        },
      });
      toast({ variant: 'success', title: t('petSpace.personalitySaved') });
    } catch (err) {
      toast({
        variant: 'error',
        title: err instanceof Error ? err.message : t('petSpace.personalitySaved'),
      });
    }
  }, [patchSettingsMut, project.id, project.settings, trimmed, toast, t]);

  const handleClear = useCallback(() => {
    setDraft('');
  }, []);

  return (
    <div className="space-y-4 rounded-xl border border-border bg-surface-raised p-5">
      <div className="space-y-2">
        <h2 className="text-base font-semibold text-foreground">{t('petSpace.personalityTitle')}</h2>
        <p className="text-sm text-muted-foreground">
          {t('petSpace.personalityDescription', { max: PET_PERSONALITY_MAX_CHARS })}
        </p>
      </div>

      <Textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={t('petSpace.personalityPlaceholder')}
        rows={12}
        maxLength={PET_PERSONALITY_MAX_CHARS + 200}
        className="font-mono text-sm leading-relaxed"
      />

      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">{t('petSpace.personalityHint')}</p>
        <p
          className={cn(
            'text-xs',
            overLimit ? 'text-red-600 dark:text-red-400' : 'text-muted-foreground',
          )}
        >
          {t('petSpace.personalityCharCount', {
            count: charCount,
            max: PET_PERSONALITY_MAX_CHARS,
          })}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          onClick={handleSave}
          disabled={!canSave}
        >
          {patchSettingsMut.isPending ? t('petSpace.personalitySaving') : t('petSpace.personalitySave')}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleClear}
          disabled={draft.length === 0 || patchSettingsMut.isPending}
        >
          {t('petSpace.personalityClear')}
        </Button>
      </div>
    </div>
  );
}
