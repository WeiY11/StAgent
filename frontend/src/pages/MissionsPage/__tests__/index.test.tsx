import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mission } from '@/api/missions';
import MissionsPage from '../index';

const mocks = vi.hoisted(() => ({
  approve: vi.fn().mockResolvedValue({ id: 'm-1', status: 'paused' }),
  mission: {
    id: 'm-1',
    goal: 'Refactor the auth module',
    expected_output: null,
    agent_id: null,
    session_id: null,
    status: 'paused',
    autonomy: 'step_confirm',
    verify_level: 'heuristic',
    plan_id: 1,
    pause_reason: 'step_confirm',
    error: null,
    started_at: '2026-08-01T00:00:00Z',
    completed_at: null,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    budget: null,
    usage: { segments: 1, tokens: 120, cost_usd: 0.02 },
    running: false,
    steps: [
      {
        step_no: 1,
        plan_id: 1,
        description: 'Survey current auth flows',
        kind: 'research',
        depends_on: [],
        wave: 0,
        status: 'passed',
        attempt: 1,
        verify_result: { passed: true, feedback: 'covers all flows' },
        segment_summary: 'found 3 flows',
        checkpoint_id: null,
        error: null,
        started_at: null,
        completed_at: null,
      },
      {
        step_no: 2,
        plan_id: 1,
        description: 'Apply the refactor',
        kind: 'code',
        depends_on: [1],
        wave: 1,
        status: 'waiting_approval',
        attempt: 0,
        verify_result: null,
        segment_summary: null,
        checkpoint_id: null,
        error: null,
        started_at: null,
        completed_at: null,
      },
    ],
  } as Mission,
  mission2: {
    id: 'm-2',
    goal: 'Ship the reports module',
    expected_output: null,
    agent_id: null,
    session_id: null,
    status: 'running',
    autonomy: 'full',
    verify_level: 'heuristic',
    plan_id: 1,
    pause_reason: null,
    error: null,
    started_at: '2026-08-01T00:00:00Z',
    completed_at: null,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    budget: null,
    usage: { segments: 0, tokens: 0, cost_usd: 0 },
    running: true,
    steps: [
      {
        step_no: 1,
        plan_id: 1,
        description: 'Draft the report schema',
        kind: 'code',
        depends_on: [],
        wave: 0,
        status: 'running',
        attempt: 1,
        verify_result: null,
        segment_summary: null,
        checkpoint_id: null,
        error: null,
        started_at: null,
        completed_at: null,
      },
    ],
  } as Mission,
}));

vi.mock('@/hooks/useMissions', () => ({
  useMissions: () => ({ data: [mocks.mission, mocks.mission2], isLoading: false }),
  useMission: (missionId: string) => ({
    data: missionId === 'm-2' ? mocks.mission2 : mocks.mission,
    isLoading: false,
  }),
  useCreateMission: () => ({ mutateAsync: vi.fn(), isPending: false }),
  usePauseMission: () => ({ mutate: vi.fn(), isPending: false }),
  useResumeMission: () => ({ mutate: vi.fn(), isPending: false }),
  useAbortMission: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteMission: () => ({ mutate: vi.fn(), isPending: false }),
  useApproveMissionStep: () => ({
    mutate: mocks.approve,
    isPending: false,
  }),
}));

function renderPage(initialEntries: string[] = ['/missions']) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={initialEntries}>
        <MissionsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('MissionsPage', () => {
  beforeEach(() => {
    mocks.approve.mockClear();
  });

  it('renders the mission list with goal and status', () => {
    renderPage();
    // The goal appears in both the list row and the detail header.
    expect(screen.getAllByText('Refactor the auth module').length).toBeGreaterThan(0);
  });

  it('deep-links to the requested mission via ?select=', async () => {
    renderPage(['/missions?select=m-2']);
    // The detail panel shows mission 2's step, not the default first mission's.
    expect(await screen.findByText('Draft the report schema')).toBeInTheDocument();
    expect(screen.queryByText('Survey current auth flows')).not.toBeInTheDocument();
  });

  it('shows the step timeline with collapsible summaries', async () => {
    renderPage();
    expect(screen.getByText('Survey current auth flows')).toBeInTheDocument();
    expect(screen.getByText('Apply the refactor')).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByText('Segment summary'));
    expect(screen.getByText('found 3 flows')).toBeInTheDocument();
  });

  it('approves a waiting_approval step from the detail panel', async () => {
    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /approve/i }));
    expect(mocks.approve).toHaveBeenCalledWith({
      missionId: 'm-1',
      stepNo: 2,
      approved: true,
    });
  });

  it('rejects a waiting_approval step', async () => {
    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /reject/i }));
    expect(mocks.approve).toHaveBeenCalledWith({
      missionId: 'm-1',
      stepNo: 2,
      approved: false,
    });
  });
});
