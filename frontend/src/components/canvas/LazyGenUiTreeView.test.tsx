import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { LazyGenUiTreeView } from './LazyGenUiTreeView';

const { moduleLoadSpy } = vi.hoisted(() => ({ moduleLoadSpy: vi.fn() }));

vi.mock('./GenUiRegistry', () => {
  moduleLoadSpy();
  return {
    GenUiTreeView: ({ tree }: { tree: { root: { nodeId: string } } }) => (
      <div data-testid="gen-ui-tree">{tree.root.nodeId}</div>
    ),
  };
});

describe('LazyGenUiTreeView', () => {
  it('does not load the renderer for an absent tree', () => {
    render(<LazyGenUiTreeView tree={null} />);

    expect(screen.queryByTestId('gen-ui-tree')).not.toBeInTheDocument();
    expect(moduleLoadSpy).not.toHaveBeenCalled();
  });

  it('loads and renders the registry for a real tree', async () => {
    render(
      <LazyGenUiTreeView
        tree={{ schemaVersion: '1', root: { nodeId: 'result-root', kind: 'Text' } }}
      />,
    );

    expect(await screen.findByTestId('gen-ui-tree')).toHaveTextContent('result-root');
    expect(moduleLoadSpy).toHaveBeenCalledTimes(1);
  });
});
