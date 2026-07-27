import { forwardRef, type TextareaHTMLAttributes } from 'react';
import { cn } from '@/lib/utils';

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  error?: string;
}

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, error, ...props }, ref) => {
    return (
      <div className="relative">
        <textarea
          ref={ref}
          className={cn(
            'w-full rounded-lg border bg-surface px-4 py-2 text-sm text-foreground shadow-xs transition-[color,background-color,border-color,box-shadow] resize-y min-h-[100px] placeholder:text-muted-foreground-tertiary',
            'focus:outline-none focus:ring-2 focus:ring-primary-500/25',
            'dark:bg-surface-elevated',
            error
              ? 'border-red-500 focus:border-red-500 focus:ring-red-500/25'
              : 'border-border hover:border-border/80 focus:border-primary-500 dark:focus:border-primary-400',
            className
          )}
          {...props}
        />
        {error && <p className="mt-1 text-xs text-red-500">{error}</p>}
      </div>
    );
  }
);

Textarea.displayName = 'Textarea';

export { Textarea };
