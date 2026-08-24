/**
 * The client reducer must agree with `StAgent/workflow/observability/gantt.py`.
 * These tests pin the event→bar mapping and the lane assignment, which are the
 * two places a divergence would silently mis-draw the timeline.
 */
import { describe, expect, it } from 'vitest';
import type { GanttBar } from '@/controllers/API/queries/observability';
import {
  applyStreamEvent,
  assignLanes,
  emptyChart,
  type ClientGantt,
  type StreamEvent,
} from '../ganttReducer';

const T0 = 1_700_000_000_000;

function bar(overrides: Partial<GanttBar> & Pick<GanttBar, 'node_id' | 'started_at_ms'>): GanttBar {
  return {
    class_type: '',
    display_name: overrides.node_id,
    finished_at_ms: null,
    duration_ms: 0,
    status: 'success',
    lane: 0,
    cached: false,
    iteration: 0,
    detail: {},
    ...overrides,
  };
}

function fold(chart: ClientGantt, events: [StreamEvent, number][]): ClientGantt {
  return events.reduce((acc, [event, now]) => applyStreamEvent(acc, event, now), chart);
}

const executing = (nodeId: string, startedAt?: number): StreamEvent => ({
  type: 'executing',
  node_id: nodeId,
  state: { node_id: nodeId, started_at_ms: startedAt ?? null, finished_at_ms: null },
});

const closes = (
  type: 'node_success' | 'node_error' | 'node_cached' | 'node_skipped',
  nodeId: string,
  span: { started_at_ms?: number; finished_at_ms?: number } = {},
): StreamEvent => ({
  type,
  node_id: nodeId,
  state: {
    node_id: nodeId,
    started_at_ms: span.started_at_ms ?? null,
    finished_at_ms: span.finished_at_ms ?? null,
  },
});

describe('assignLanes', () => {
  it.each([
    {
      name: 'serial dependencies share one lane',
      bars: [
        bar({ node_id: 'a', started_at_ms: T0, finished_at_ms: T0 + 100 }),
        bar({ node_id: 'b', started_at_ms: T0 + 100, finished_at_ms: T0 + 200 }),
      ],
      lanes: 1,
      expected: { a: 0, b: 0 },
    },
    {
      name: 'overlapping branches stagger into separate lanes',
      bars: [
        bar({ node_id: 'a', started_at_ms: T0, finished_at_ms: T0 + 300 }),
        bar({ node_id: 'b', started_at_ms: T0 + 50, finished_at_ms: T0 + 200 }),
        bar({ node_id: 'c', started_at_ms: T0 + 60, finished_at_ms: T0 + 90 }),
      ],
      lanes: 3,
      expected: { a: 0, b: 1, c: 2 },
    },
    {
      name: 'a lane is reused once its previous bar has finished',
      bars: [
        bar({ node_id: 'a', started_at_ms: T0, finished_at_ms: T0 + 100 }),
        bar({ node_id: 'b', started_at_ms: T0 + 50, finished_at_ms: T0 + 150 }),
        bar({ node_id: 'c', started_at_ms: T0 + 100, finished_at_ms: T0 + 200 }),
      ],
      lanes: 2,
      expected: { a: 0, b: 1, c: 0 },
    },
    {
      name: 'an open bar occupies its lane forever',
      bars: [
        bar({ node_id: 'a', started_at_ms: T0, finished_at_ms: null }),
        bar({ node_id: 'b', started_at_ms: T0 + 500, finished_at_ms: T0 + 600 }),
      ],
      lanes: 2,
      expected: { a: 0, b: 1 },
    },
  ])('$name', ({ bars, lanes, expected }) => {
    const laid = assignLanes(bars);
    expect(laid.laneCount).toBe(lanes);
    for (const [nodeId, lane] of Object.entries(expected)) {
      expect(laid.bars.find((b) => b.node_id === nodeId)?.lane).toBe(lane);
    }
  });

  it('never reports fewer than one lane', () => {
    expect(assignLanes([]).laneCount).toBe(1);
  });
});

describe('applyStreamEvent', () => {
  it('ignores an event with no type', () => {
    const chart = emptyChart('p1', T0);
    expect(applyStreamEvent(chart, { type: '' }, T0)).toBe(chart);
  });

  it('returns the same reference when nothing was touched', () => {
    const chart = emptyChart('p1', T0);
    // No bar is open, so there is nothing for node_success to close.
    expect(applyStreamEvent(chart, closes('node_success', 'ghost'), T0 + 10)).toBe(chart);
  });

  it('seeds the chart origin and the node catalogue from execution_start', () => {
    const chart = applyStreamEvent(
      emptyChart('p1', T0 + 9_999),
      {
        type: 'execution_start',
        data: {
          started_at_ms: T0,
          node_total: 2,
          flow_name: 'Demo flow',
          nodes: { a: { class_type: 'Foo', display_name: 'Alpha' } },
        },
      },
      T0 + 9_999,
    );

    expect(chart.started_at_ms).toBe(T0);
    expect(chart.node_total).toBe(2);
    expect(chart.flow_name).toBe('Demo flow');
    expect(chart.node_meta?.a?.display_name).toBe('Alpha');
  });

  it('labels a streamed bar from the catalogue', () => {
    const chart = fold(emptyChart('p1', T0), [
      [
        {
          type: 'execution_start',
          data: { started_at_ms: T0, nodes: { a: { class_type: 'Foo', display_name: 'Alpha' } } },
        },
        T0,
      ],
      [executing('a', T0), T0],
    ]);

    expect(chart.bars[0].display_name).toBe('Alpha');
    expect(chart.bars[0].class_type).toBe('Foo');
  });

  it('opens a running bar on executing and closes it on node_success', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [closes('node_success', 'a', { finished_at_ms: T0 + 400 }), T0 + 400],
    ]);

    expect(chart.bars).toHaveLength(1);
    expect(chart.bars[0]).toMatchObject({
      node_id: 'a',
      status: 'success',
      started_at_ms: T0,
      finished_at_ms: T0 + 400,
      duration_ms: 400,
      iteration: 0,
    });
  });

  it('records the message on node_error', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [
        {
          type: 'node_error',
          node_id: 'a',
          state: { node_id: 'a', finished_at_ms: T0 + 50 },
          data: { error: 'boom' },
        },
        T0 + 50,
      ],
    ]);

    expect(chart.bars[0].status).toBe('error');
    expect(chart.bars[0].detail.error).toBe('boom');
  });

  it('gives a cache hit its own short, flagged bar', () => {
    const chart = applyStreamEvent(
      emptyChart('p1', T0),
      closes('node_cached', 'a', { started_at_ms: T0 + 10, finished_at_ms: T0 + 12 }),
      T0 + 12,
    );

    expect(chart.bars).toHaveLength(1);
    expect(chart.bars[0]).toMatchObject({ status: 'cached', cached: true, duration_ms: 2 });
  });

  it('greys out a skipped node instead of omitting it', () => {
    const chart = applyStreamEvent(
      emptyChart('p1', T0),
      closes('node_skipped', 'a', { started_at_ms: T0, finished_at_ms: T0 }),
      T0,
    );

    expect(chart.bars[0].status).toBe('skipped');
    expect(chart.bars[0].cached).toBe(false);
  });

  it('leaves a blocked bar hanging so the human wait is visible', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [{ type: 'node_blocked', node_id: 'a', state: { node_id: 'a', started_at_ms: T0 + 30 } }, T0 + 30],
    ]);

    expect(chart.bars[0].status).toBe('blocked');
    expect(chart.bars[0].finished_at_ms).toBeNull();
    expect(chart.bars[0].detail.compute_ms).toBe(30);
  });

  it('marks a blocked bar as resumed once it succeeds', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [{ type: 'node_blocked', node_id: 'a', state: { node_id: 'a', started_at_ms: T0 + 30 } }, T0 + 30],
      [closes('node_success', 'a', { finished_at_ms: T0 + 5_000 }), T0 + 5_000],
    ]);

    expect(chart.bars[0]).toMatchObject({
      status: 'success',
      finished_at_ms: T0 + 5_000,
      duration_ms: 5_000,
    });
    expect(chart.bars[0].detail.resumed).toBe(true);
  });

  it('counts a re-announced span as a retry, not a second run', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [executing('a', T0), T0 + 10],
      [executing('a', T0), T0 + 20],
    ]);

    expect(chart.bars).toHaveLength(1);
    expect(chart.bars[0].detail.retry).toBe(2);
  });

  it('opens a second bar for a loop re-entry and clamps the first', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [closes('node_success', 'a', { finished_at_ms: T0 + 100 }), T0 + 100],
      [executing('a', T0 + 200), T0 + 200],
    ]);

    expect(chart.bars).toHaveLength(2);
    expect(chart.bars.map((b) => b.iteration)).toEqual([0, 1]);
    expect(chart.bars[1].status).toBe('running');
    expect(chart.bars[1].finished_at_ms).toBeNull();
  });

  it('clamps an open bar when a new span starts without a close event', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [executing('a', T0 + 300), T0 + 300],
    ]);

    expect(chart.bars).toHaveLength(2);
    // The dropped close must not be reinterpreted as a failure.
    expect(chart.bars[0]).toMatchObject({ status: 'running', finished_at_ms: T0 + 300 });
  });

  it('ends the chart and clamps open bars without changing their status', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [{ type: 'node_blocked', node_id: 'a', state: { node_id: 'a', started_at_ms: T0 + 10 } }, T0 + 10],
      [executing('b', T0 + 20), T0 + 20],
      [{ type: 'execution_error', data: { finished_at_ms: T0 + 900 } }, T0 + 900],
    ]);

    expect(chart.finished_at_ms).toBe(T0 + 900);
    expect(chart.status).toBe('error');
    expect(chart.bars.every((b) => b.finished_at_ms !== null)).toBe(true);
    expect(chart.bars.find((b) => b.node_id === 'a')?.status).toBe('blocked');
    expect(chart.bars.find((b) => b.node_id === 'b')?.status).toBe('running');
  });

  it('keeps the chart open on execution_blocked', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [{ type: 'execution_blocked', data: {} }, T0 + 5],
    ]);

    expect(chart.finished_at_ms).toBeNull();
    expect(chart.bars[0].finished_at_ms).toBeNull();
  });

  it('records progress on the open bar', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [{ type: 'progress', node_id: 'a', state: { node_id: 'a', value: 3, max: 4 } }, T0 + 5],
    ]);

    expect(chart.bars[0].detail.progress).toBeCloseTo(0.75);
  });

  it('re-lays lanes as parallel branches appear', () => {
    const chart = fold(emptyChart('p1', T0), [
      [executing('a', T0), T0],
      [executing('b', T0 + 10), T0 + 10],
      [executing('c', T0 + 20), T0 + 20],
    ]);

    expect(chart.lane_count).toBe(3);
    expect(chart.bars.map((b) => b.lane).sort()).toEqual([0, 1, 2]);
  });

  it('falls back to the arrival clock when the event carries no span', () => {
    const chart = applyStreamEvent(emptyChart('p1', T0), executing('a'), T0 + 42);
    expect(chart.bars[0].started_at_ms).toBe(T0 + 42);
  });

  it('ignores a node event with no resolvable node id', () => {
    const chart = emptyChart('p1', T0);
    expect(applyStreamEvent(chart, { type: 'executing' }, T0)).toBe(chart);
  });
});
