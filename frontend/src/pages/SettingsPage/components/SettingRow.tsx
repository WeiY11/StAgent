import { memo } from 'react';

/**
 * A single settings row with label + description on the left, control on the right.
 * Extracted from SettingsPage to be reusable across tab panels.
 */
export const SettingRow = memo(function SettingRow({
  label,
  description,
  children,
}: {
  label: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0">
        <p className="font-medium text-gray-900 dark:text-white">{label}</p>
        <p className="text-sm text-gray-500 dark:text-gray-400">{description}</p>
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  );
});
