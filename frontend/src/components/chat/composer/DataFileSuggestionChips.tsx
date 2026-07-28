import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Sparkles, X } from 'lucide-react';
import { useChatDraftStore } from '@/stores/chatDraft';

/** Spreadsheet-like attachments that trigger the analysis suggestion chips. */
const DATA_FILE_RE = /\.(csv|xlsx|xls)$/i;

const CHIP_KEYS = ['overview', 'clean', 'chart'] as const;

interface DataFileSuggestionChipsProps {
  files: File[];
}

/**
 * Suggestion chips shown above the composer when a CSV/Excel attachment is
 * staged. Clicking a chip only fills the composer draft (no auto-send).
 */
export function DataFileSuggestionChips({ files }: DataFileSuggestionChipsProps) {
  const { t } = useTranslation();
  const [dismissed, setDismissed] = useState(false);
  const hasDataFile = useMemo(
    () => files.some((f) => DATA_FILE_RE.test(f.name)),
    [files],
  );

  // Re-arm the hint once every data attachment is removed.
  useEffect(() => {
    if (!hasDataFile) setDismissed(false);
  }, [hasDataFile]);

  if (!hasDataFile || dismissed) return null;

  return (
    <div className="pb-2 flex items-center flex-wrap gap-1.5">
      <span className="flex items-center gap-1 text-xs text-muted-foreground-tertiary">
        <Sparkles className="w-3 h-3" />
        {t('chat.dataUpload.hint', { defaultValue: 'Data file detected — try:' })}
      </span>
      {CHIP_KEYS.map((key) => (
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
      <button
        type="button"
        onClick={() => setDismissed(true)}
        className="p-0.5 rounded-full text-muted-foreground-tertiary hover:bg-border-subtle hover:text-foreground transition-colors"
        aria-label={t('chat.dataUpload.dismissAria', {
          defaultValue: 'Dismiss data suggestions',
        })}
      >
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}
