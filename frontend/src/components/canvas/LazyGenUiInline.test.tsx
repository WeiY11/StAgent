import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useGenUiStore } from '@/stores/genUi';
import { LazyGenUiInline } from './LazyGenUiInline';

const { moduleLoadSpy } = vi.hoisted(() => ({ moduleLoadSpy: vi.fn() }));

vi.mock('./GenUiInline', () => {
  moduleLoadSpy();
  return {
    GenUiInline: ({ messageId }: { messageId: string }) => (
      <div data-testid="gen-ui-inline">{messageId}</div>
    ),
  };
});

describe('LazyGenUiInline', () => {
  beforeEach(() => {
    moduleLoadSpy.mockClear();
    useGenUiStore.setState({
      trees: {},
      sourceToolCallByMessage: {},
      errorTrees: new Set<string>(),
    });
  });

  it('loads the heavy renderer only after the message receives a GenUI tree', async () => {
    render(<LazyGenUiInline sessionId="session-1" messageId="message-1" />);

    expect(screen.queryByTestId('gen-ui-inline')).not.toBeInTheDocument();
    expect(moduleLoadSpy).not.toHaveBeenCalled();

    act(() => {
      useGenUiStore.getState().setFromStream('session-1', 'message-1', {
        tree: {
          schemaVersion: '1',
          root: { nodeId: 'root', kind: 'Text', props: { value: 'ready' } },
        },
      });
    });

    expect(await screen.findByTestId('gen-ui-inline')).toHaveTextContent('message-1');
    expect(moduleLoadSpy).toHaveBeenCalledTimes(1);
  });
});
