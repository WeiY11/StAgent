import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Message } from '@/types/chat';
import { ToastProvider, Toaster } from '@/components/ui/Toaster';
import { UserMessage } from './UserMessage';

const mocks = vi.hoisted(() => ({
  forkSession: vi.fn(),
  togglePinMessage: vi.fn(),
}));

vi.mock('@/stores/chat', () => {
  const state = {
    forkSession: mocks.forkSession,
    togglePinMessage: mocks.togglePinMessage,
    sessions: [] as Array<{ id: string; pinnedMessageIds?: string[] }>,
  };
  return {
    useChatStore: (selector: (s: typeof state) => unknown) => selector(state),
  };
});

const SESSION_ID = '22222222-2222-4222-8222-222222222222';
const message: Message = {
  id: '11111111-1111-4111-8111-111111111111',
  role: 'user',
  content: 'hello world',
  createdAt: '2026-08-14T00:00:00.000Z',
};

function renderMessage(sessionId: string | null = SESSION_ID) {
  return render(
    <ToastProvider>
      <Toaster />
      <UserMessage message={message} sessionId={sessionId} />
    </ToastProvider>,
  );
}

describe('UserMessage fork action', () => {
  beforeEach(() => {
    mocks.forkSession.mockReset();
    mocks.togglePinMessage.mockReset();
  });

  it('calls forkSession with the message id and toasts on success', async () => {
    mocks.forkSession.mockResolvedValue('33333333-3333-4333-8333-333333333333');
    renderMessage();

    fireEvent.click(screen.getByRole('button', { name: /branch from here/i }));

    await waitFor(() => {
      expect(mocks.forkSession).toHaveBeenCalledWith(SESSION_ID, message.id);
    });
    expect(await screen.findByText(/Branched chat created/i)).toBeInTheDocument();
  });

  it('toasts an error when the fork request fails', async () => {
    mocks.forkSession.mockResolvedValue(null);
    renderMessage();

    fireEvent.click(screen.getByRole('button', { name: /branch from here/i }));

    expect(await screen.findByText(/Failed to create branch/i)).toBeInTheDocument();
  });

  it('hides the branch button without a session', () => {
    renderMessage(null);
    expect(screen.queryByRole('button', { name: /branch from here/i })).not.toBeInTheDocument();
  });
});
