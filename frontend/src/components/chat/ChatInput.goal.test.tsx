import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useChatStore } from '@/stores/chat';
import { useChatDraftStore } from '@/stores/chatDraft';
import type { ChatSession } from '@/types/chat';
import { ChatInput } from './ChatInput';

const mocks = vi.hoisted(() => ({
  createMission: vi.fn(),
  onSend: vi.fn().mockResolvedValue(undefined),
  streamBusy: false,
}));

vi.mock('@/stores/chat', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    isChatStreamBusyForSession: () => mocks.streamBusy,
  };
});

vi.mock('@/hooks/useMissions', () => ({
  useCreateMission: () => ({ mutateAsync: mocks.createMission, isPending: false }),
}));

vi.mock('@/hooks/useSkills', () => ({
  useSkillsList: () => ({ data: undefined }),
}));

vi.mock('@/hooks/useChat', () => ({
  useSessionAuthorizedPaths: () => ({ data: undefined }),
  useAddAuthorizedPath: () => ({ mutateAsync: vi.fn() }),
  useRemoveAuthorizedPath: () => ({ mutateAsync: vi.fn() }),
}));

// Leaf components that would fire model/context API queries on mount.
vi.mock('./composer/ModelSelector', () => ({ ModelSelector: () => null }));
vi.mock('./composer/ContextUsagePopover', () => ({
  ContextUsagePopover: () => null,
}));
vi.mock('./CameraCaptureModal', () => ({ CameraCaptureModal: () => null }));
vi.mock('./LocalFolderBrowser', () => ({ LocalFolderBrowser: () => null }));

const SESSION_ID = '11111111-1111-1111-1111-111111111111';

function MissionsProbe() {
  const [params] = useSearchParams();
  return <div data-testid="missions-probe">{params.get('select') ?? ''}</div>;
}

function renderComposer() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/chat']}>
        <Routes>
          <Route path="/chat" element={<ChatInput onSend={mocks.onSend} onStop={vi.fn()} />} />
          <Route path="/missions" element={<MissionsProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function setDraft(body: string) {
  act(() => {
    useChatDraftStore.setState({ composerBody: body });
  });
}

async function clickSend() {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /^(发送|Send)$/ }));
}

describe('ChatInput /goal command', () => {
  beforeEach(() => {
    mocks.createMission.mockReset();
    mocks.onSend.mockClear();
    mocks.streamBusy = false;
    mocks.createMission.mockResolvedValue({ id: 'mission-1' });
    useChatDraftStore.setState({
      composerBody: '',
      composerFiles: [],
      composerFileRefs: [],
    });
    useChatStore.setState({
      error: null,
      currentSessionId: SESSION_ID,
      sessions: [{ id: SESSION_ID, isPending: false } as ChatSession],
    });
  });

  it('creates a linked mission instead of sending a chat message', async () => {
    renderComposer();
    act(() => {
      useChatDraftStore.setState({
        composerBody: '/goal Refactor the auth module and add tests',
        composerFiles: [new File(['x'], 'notes.csv')],
        composerFileRefs: [
          { clientId: 'ref-1', kind: 'skill', token: '[[skill:demo]]', label: 'demo' },
        ],
      });
    });
    await clickSend();

    await waitFor(() => {
      expect(mocks.createMission).toHaveBeenCalledTimes(1);
    });
    expect(mocks.createMission).toHaveBeenCalledWith({
      goal: 'Refactor the auth module and add tests',
      session_id: SESSION_ID,
      autonomy: 'full',
      verify_level: 'heuristic',
    });
    expect(mocks.onSend).not.toHaveBeenCalled();
    // The whole draft is consumed (body, attachments, refs), like a normal send.
    await waitFor(() => {
      expect(useChatDraftStore.getState().composerBody).toBe('');
    });
    expect(useChatDraftStore.getState().composerFiles).toHaveLength(0);
    expect(useChatDraftStore.getState().composerFileRefs).toHaveLength(0);
    // Navigation lands on the missions page with the new mission selected.
    expect(await screen.findByTestId('missions-probe')).toHaveTextContent('mission-1');
  });

  it('rejects /goal without a goal description', async () => {
    renderComposer();
    setDraft('/goal');
    await clickSend();

    expect(mocks.createMission).not.toHaveBeenCalled();
    expect(mocks.onSend).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(useChatStore.getState().error).toBeTruthy();
    });
    expect(useChatDraftStore.getState().composerBody).toBe('/goal');
  });

  it('rejects a goal longer than the API limit', async () => {
    renderComposer();
    setDraft(`/goal ${'x'.repeat(4001)}`);
    await clickSend();

    expect(mocks.createMission).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(useChatStore.getState().error).toBeTruthy();
    });
  });

  it('keeps the draft when mission creation fails', async () => {
    mocks.createMission.mockRejectedValue(new Error('boom'));
    renderComposer();
    setDraft('/goal Ship it');
    await clickSend();

    await waitFor(() => {
      expect(useChatStore.getState().error).toBe('boom');
    });
    expect(mocks.onSend).not.toHaveBeenCalled();
    expect(useChatDraftStore.getState().composerBody).toBe('/goal Ship it');
  });

  it('does not steer a running turn with /goal text', async () => {
    mocks.streamBusy = true;
    renderComposer();
    setDraft('/goal Ship it');

    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox'), '{Enter}');

    expect(mocks.createMission).not.toHaveBeenCalled();
    expect(mocks.onSend).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(useChatStore.getState().error).toBeTruthy();
    });
  });

  it('does not intercept normal messages or /goal-prefixed words', async () => {
    renderComposer();
    setDraft('/goals is not the goal command');
    await clickSend();

    expect(mocks.createMission).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(mocks.onSend).toHaveBeenCalledWith(
        '/goals is not the goal command',
        undefined,
        null,
        undefined,
        null,
        undefined,
      );
    });
  });
});
