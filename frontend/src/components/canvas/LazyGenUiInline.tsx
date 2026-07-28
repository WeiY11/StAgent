import { lazy, Suspense } from 'react';
import { genUiTreeKey, useGenUiStore } from '@/stores/genUi';

const GenUiInline = lazy(() =>
  import('./GenUiInline').then((module) => ({ default: module.GenUiInline })),
);

interface LazyGenUiInlineProps {
  sessionId: string;
  messageId: string;
  className?: string;
}

/**
 * Keep the GenUI renderer out of ordinary chat bundles until a message has a tree.
 *
 * ``GenUiInline`` pulls in the renderer registry, chart components, export helpers,
 * and capture code. Most messages never use them, so checking the lightweight store
 * first avoids downloading and evaluating that graph on the normal chat path.
 */
export function LazyGenUiInline({
  sessionId,
  messageId,
  className,
}: LazyGenUiInlineProps) {
  const hasTree = useGenUiStore(
    (state) => Boolean(state.trees[genUiTreeKey(sessionId, messageId)]),
  );

  if (!hasTree) return null;

  return (
    <Suspense fallback={null}>
      <GenUiInline sessionId={sessionId} messageId={messageId} className={className} />
    </Suspense>
  );
}
