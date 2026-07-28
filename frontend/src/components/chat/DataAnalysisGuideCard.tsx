import { useTranslation } from 'react-i18next';
import { BarChart3, X } from 'lucide-react';
import { useChatDraftStore } from '@/stores/chatDraft';

/** Example prompts reuse the data-upload chip copy for consistency. */
const EXAMPLE_KEYS = ['overview', 'clean', 'chart'] as const;

interface DataAnalysisGuideCardProps {
  onDismiss: () => void;
}

/**
 * Guided empty-state card for the `/chat?intent=data-analysis` deep link.
 * Clicking an example only fills the composer draft (no auto-send).
 */
export function DataAnalysisGuideCard({ onDismiss }: DataAnalysisGuideCardProps) {
  const { t } = useTranslation();

  return (
    <div className="mb-2 rounded-xl border border-border bg-surface p-4 shadow-sm">
      <div className="flex items-start gap-3">
        <div className="p-2 rounded-lg bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-300">
          <BarChart3 className="w-5 h-5" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="font-medium text-foreground">
            {t('chat.dataAnalysisGuide.title', { defaultValue: 'Data analysis' })}
          </p>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {t('chat.dataAnalysisGuide.description', {
              defaultValue:
                'Upload a CSV / Excel file and I will profile, clean, aggregate and chart it. Or click an example below to fill the composer:',
            })}
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {EXAMPLE_KEYS.map((key) => (
              <button
                key={key}
                type="button"
                onClick={() =>
                  useChatDraftStore
                    .getState()
                    .pushInsert(t(`chat.dataUpload.chips.${key}.prompt`))
                }
                className="px-2.5 py-1 rounded-full bg-surface-sunken text-muted-foreground hover:text-foreground hover:bg-border-subtle text-xs font-medium transition-colors"
              >
                {t(`chat.dataUpload.chips.${key}.label`)}
              </button>
            ))}
          </div>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="p-1 rounded-lg text-muted-foreground-tertiary hover:text-foreground hover:bg-surface-sunken transition-colors"
          aria-label={t('chat.dataAnalysisGuide.dismissAria', {
            defaultValue: 'Dismiss data analysis guide',
          })}
        >
          <X className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
