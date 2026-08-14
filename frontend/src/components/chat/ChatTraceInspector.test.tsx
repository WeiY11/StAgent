import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type * as TracesModule from '@/api/traces';
import { ChatTraceInspector } from './ChatTraceInspector';

type TraceDetail = TracesModule.TraceDetail;
type TraceSpanDetail = TracesModule.TraceSpanDetail;
type TraceSummary = TracesModule.TraceSummary;

const mocks = vi.hoisted(() => ({
  getSpan: vi.fn(),
  traceId: 'trace-1',
}));

vi.mock('@/api/traces', async (importOriginal) => {
  const actual = await importOriginal<typeof TracesModule>();
  const summary: TraceSummary = {
    trace_id: mocks.traceId,
    scope: 'chat_turn',
    agent_name: 'default_agent',
    model: 'm',
    status: 'completed',
    latency_ms: 5,
    input_tokens: 1,
    output_tokens: 1,
    cache_read_tokens: 0,
    cache_miss_tokens: 0,
    total_cost_usd: 0,
    tool_call_count: 0,
    llm_call_count: 1,
  };
  return {
    ...actual,
    tracesApi: {
      ...actual.tracesApi,
      listSession: vi.fn().mockResolvedValue([summary]),
      get: vi.fn(),
      getSpan: mocks.getSpan,
    },
  };
});

const traceId = mocks.traceId;

function makeDetail(): TraceDetail {
  const now = '2026-08-14T00:00:00.000Z';
  return {
    trace: {
      trace_id: traceId,
      scope: 'chat_turn',
      agent_name: 'default_agent',
      model: 'm',
      status: 'completed',
      latency_ms: 5,
      input_tokens: 1,
      output_tokens: 1,
      cache_read_tokens: 0,
      cache_miss_tokens: 0,
      total_cost_usd: 0,
      tool_call_count: 0,
      llm_call_count: 1,
    },
    spans: [
      {
        span_id: 'span-1',
        trace_id: traceId,
        seq: 1,
        kind: 'llm',
        name: 'llm-call',
        status: 'ok',
        started_at: now,
        ended_at: now,
        latency_ms: 4,
        attrs: {},
        input_preview: 'in-preview',
        output_preview: 'out-preview',
      },
    ],
    tree: [],
  };
}

async function renderInspector() {
  const { tracesApi } = await import('@/api/traces');
  vi.mocked(tracesApi.get).mockResolvedValue(makeDetail());
  render(<ChatTraceInspector sessionId="session-1" />);
  const row = await screen.findByRole('button', { name: /llm-call/i });
  fireEvent.click(row);
}

describe('ChatTraceInspector span payloads', () => {
  beforeEach(() => {
    mocks.getSpan.mockReset();
  });

  it('loads the full span payload into a modal', async () => {
    await renderInspector();

    const detail: TraceSpanDetail = {
      span_id: 'span-1',
      trace_id: traceId,
      seq: 1,
      kind: 'llm',
      name: 'llm-call',
      status: 'ok',
      latency_ms: 4,
      attrs: {},
      input_preview: 'in-preview',
      output_preview: 'out-preview',
      input_full: 'the full untruncated input',
      output_full: 'the full untruncated output',
    };
    mocks.getSpan.mockResolvedValue(detail);

    fireEvent.click(await screen.findByRole('button', { name: /view full/i }));

    expect(mocks.getSpan).toHaveBeenCalledWith(traceId, 'span-1');
    expect(await screen.findByText('the full untruncated input')).toBeInTheDocument();
    expect(screen.getByText('the full untruncated output')).toBeInTheDocument();
    expect(screen.getByText('Full input')).toBeInTheDocument();
    expect(screen.getByText('Full output')).toBeInTheDocument();
  });

  it('surfaces an inline error when the span detail request fails', async () => {
    await renderInspector();

    mocks.getSpan.mockRejectedValue(new Error('boom'));

    fireEvent.click(await screen.findByRole('button', { name: /view full/i }));

    await waitFor(() => {
      expect(screen.getByText(/Unable to load full payload/i)).toBeInTheDocument();
    });
  });
});
