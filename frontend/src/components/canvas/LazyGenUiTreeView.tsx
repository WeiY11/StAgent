import { lazy, Suspense, type Ref } from 'react';
import type { GenUiTreeV1 } from '@/types/genUi';

const GenUiTreeView = lazy(() =>
  import('./GenUiRegistry').then((module) => ({ default: module.GenUiTreeView })),
);

interface LazyGenUiTreeViewProps {
  tree: GenUiTreeV1 | null | undefined;
  contentRef?: Ref<HTMLDivElement>;
  sessionId?: string;
  messageId?: string;
  jsEnabled?: boolean;
  variant?: 'default' | 'inline';
}

/** Load the GenUI registry only when a real render tree reaches the surface. */
export function LazyGenUiTreeView({ tree, ...props }: LazyGenUiTreeViewProps) {
  if (!tree?.root) return null;

  return (
    <Suspense fallback={null}>
      <GenUiTreeView tree={tree} {...props} />
    </Suspense>
  );
}
