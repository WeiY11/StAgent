import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { isUuid } from '@/lib/utils/utils';
import {
  namespacedPersistName,
  registerNamespacedStore,
} from '@/lib/utils/persistNamespace';
import { getChatProjectHeaders, useChatProjectStore } from './chatProjects';
import { createSessionSlice } from './chat/session-slice';
import { createMessagesSlice } from './chat/messages-slice';
import { createStreamingSlice } from './chat/streaming-slice';
import { createFeaturesSlice } from './chat/features-slice';
import type { ChatStore } from './chat/types';

// Re-export helpers used by external consumers.
export { dedupeMessagesByIdPreserveOrder, isChatStreamBusyForSession } from './chatHelpers';

// Re-export the ChatStore type for external consumers that need it.
export type { ChatStore } from './chat/types';

const CHAT_STORE_BASE = 'StAgent-chat';
registerNamespacedStore(CHAT_STORE_BASE);

/** Resolve the project id for a session from the current store state. */
function projectIdForSession(sessionId: string): string | null {
  const session = useChatStore.getState().sessions.find((s) => s.id === sessionId);
  return session?.projectId ?? null;
}

/** Project unlock header for session-scoped chat API calls. */
export function getSessionProjectHeaders(sessionId: string): Record<string, string> | undefined {
  return getChatProjectHeaders(projectIdForSession(sessionId));
}

/** False when the session belongs to a password-protected project without a stored unlock token. */
export function isSessionProjectUnlocked(sessionId: string): boolean {
  const projectId = projectIdForSession(sessionId);
  if (!projectId) return true;
  return useChatProjectStore.getState().isProjectUnlocked(projectId);
}

export const useChatStore = create<ChatStore>()(
  persist(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ((set: any, get: any, api: any) => ({
      ...createSessionSlice(set, get, api),
      ...createMessagesSlice(set, get, api),
      ...createStreamingSlice(set, get, api),
      ...createFeaturesSlice(set, get, api),
    })) as any, // eslint-disable-line @typescript-eslint/no-explicit-any
    {
      name: namespacedPersistName(CHAT_STORE_BASE),
      partialize: (state) => ({
        sessions: state.sessions.slice(0, 50),
        currentSessionId: state.currentSessionId,
      }),
      onRehydrateStorage: () => (state) => {
        if (state && state.currentSessionId && !isUuid(state.currentSessionId)) {
          state.currentSessionId = null;
        }
      },
    }
  )
);
