/**
 * Rendering contract for the visual core: one element per bar, positioned by
 * time, coloured by status, with the approximate-timeline warning surfaced for
 * replayed legacy rows.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import type { ExecutionGantt, GanttBar } from '@/controllers/API/queries/observability';
import {
  GanttTimeline,
  GanttLegend,
  GANTT_STATUS_COLOR,
  formatOffset,
  barKey,
} from '../GanttTimeline';

const T0 = 1_700_000_000_000;

function bar(overrides: Partial<GanttBar> & Pick<GanttBar, 'node_id'>): GanttBar {
  return {
    class_type: 'Foo',
    display_name: overrides.node_id,
    started_at_ms: T0,
    finished_at_ms: T0 + 100,
    duration_ms: 100,
    status: 'success',
    lane: 0,
    cached: false,
    iteration: 0,
    detail: {},
    ...overrides,
  };
}

function chart(bars: GanttBar[], overrides: Partial<ExecutionGantt> = {}): ExecutionGantt {
  return {
    prompt_id: 'p1',
    execution_id: 'e1',
    flow_id: 'f1',
    flow_name: 'Demo flow',
    started_at_ms: T0,
    finished_at_ms: T0 + 1_000,
    status: 'success',
    lane_count: Math.max(1, ...bars.map((b) => b.lane + 1)),
    approximate: false,
    node_total: bars.length,
    bars,
    server_now_ms: T0 + 1_000,
    ...overrides,
  };
}

describe('formatOffset', () => {
  it.each([
    [0, '0ms'],
    [999, '999ms'],
    [1_000, '1.0s'],
    [9_500, '9.5s'],
    [45_000, '45s'],
    [90_000, '1m30s'],
    [120_000, '2m'],
    [3_720_000, '1h2m'],
  ])('%ims → %s', (input, expected) => {
    expect(formatOffset(input)).toBe(expected);
  });

  it('clamps a negative span to zero', () => {
    expect(formatOffset(-50)).toBe('0ms');
  });
});

describe('GanttTimeline', () => {
  it('renders one bar per node execution', () => {
    render(
      <GanttTimeline
        chart={chart([
          bar({ node_id: 'a' }),
          bar({ node_id: 'b', started_at_ms: T0 + 100, finished_at_ms: T0 + 300, lane: 0 }),
        ])}
        nowMs={T0 + 1_000}
      />,
    );

    const bars = screen.getAllByTestId('gantt-bar');
    expect(bars).toHaveLength(2);
    expect(bars.map((el) => el.getAttribute('data-node-id'))).toEqual(['a', 'b']);
  });

  it.each([
    ['running', GANTT_STATUS_COLOR.running],
    ['success', GANTT_STATUS_COLOR.success],
    ['error', GANTT_STATUS_COLOR.error],
    ['blocked', GANTT_STATUS_COLOR.blocked],
    ['cached', GANTT_STATUS_COLOR.cached],
    ['skipped', GANTT_STATUS_COLOR.skipped],
  ] as const)('colours a %s bar', (status, colour) => {
    render(
      <GanttTimeline
        chart={chart([bar({ node_id: 'a', status })])}
        nowMs={T0 + 1_000}
      />,
    );

    expect(screen.getByTestId('gantt-bar')).toHaveStyle({ backgroundColor: colour });
  });

  it('flags an open bar and draws it up to the current clock', () => {
    render(
      <GanttTimeline
        chart={chart([bar({ node_id: 'a', status: 'running', finished_at_ms: null })], {
          finished_at_ms: null,
        })}
        nowMs={T0 + 400}
      />,
    );

    const el = screen.getByTestId('gantt-bar');
    expect(el).toHaveAttribute('data-open', 'true');
    // 400ms of a 500ms floor span.
    expect(el.style.left).toBe('0%');
    expect(el.style.width).toContain('80%');
  });

  it('positions a bar by its offset from the chart origin', () => {
    render(
      <GanttTimeline
        chart={chart([bar({ node_id: 'b', started_at_ms: T0 + 500, finished_at_ms: T0 + 750 })])}
        nowMs={T0 + 1_000}
      />,
    );

    const el = screen.getByTestId('gantt-bar');
    expect(el.style.left).toBe('50%');
    expect(el.style.width).toContain('25%');
  });

  it('places each bar on its assigned lane row', () => {
    render(
      <GanttTimeline
        chart={chart([
          bar({ node_id: 'a', lane: 0 }),
          bar({ node_id: 'b', lane: 1, started_at_ms: T0 + 10 }),
        ])}
        nowMs={T0 + 1_000}
      />,
    );

    const [first, second] = screen.getAllByTestId('gantt-bar');
    expect(parseInt(second.style.top, 10)).toBeGreaterThan(parseInt(first.style.top, 10));
  });

  it('shows the empty state when no node has run', () => {
    render(<GanttTimeline chart={chart([])} nowMs={T0} />);

    expect(screen.getByTestId('gantt-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('gantt-bar')).not.toBeInTheDocument();
  });

  it('marks the chart approximate when the timestamps were reconstructed', () => {
    render(
      <GanttTimeline chart={chart([bar({ node_id: 'a' })], { approximate: true })} nowMs={T0} />,
    );

    expect(screen.getByTestId('gantt-timeline')).toHaveAttribute('data-approximate', 'true');
  });

  it('hands the clicked bar to the caller', () => {
    const onSelectBar = vi.fn();
    const target = bar({ node_id: 'a', iteration: 2 });
    render(
      <GanttTimeline chart={chart([target])} nowMs={T0 + 1_000} onSelectBar={onSelectBar} />,
    );

    fireEvent.click(screen.getByTestId('gantt-bar'));
    expect(onSelectBar).toHaveBeenCalledWith(target);
    expect(barKey(target)).toBe('a#2');
  });

  it('reveals node timing on hover', () => {
    render(
      <GanttTimeline
        chart={chart([bar({ node_id: 'a', display_name: 'Alpha', detail: { retry: 2 } })])}
        nowMs={T0 + 1_000}
      />,
    );

    expect(screen.queryByTestId('gantt-tooltip')).not.toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByTestId('gantt-bar'));

    const tip = screen.getByTestId('gantt-tooltip');
    expect(tip).toHaveTextContent('Alpha');
    expect(tip).toHaveTextContent('100ms');

    fireEvent.mouseLeave(screen.getByTestId('gantt-bar'));
    expect(screen.queryByTestId('gantt-tooltip')).not.toBeInTheDocument();
  });

  it('drops the axis, labels and tooltip in compact mode', () => {
    render(
      <GanttTimeline
        chart={chart([bar({ node_id: 'a', display_name: 'Alpha' })])}
        nowMs={T0 + 1_000}
        compact
      />,
    );

    expect(screen.getByTestId('gantt-bar')).toBeInTheDocument();
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByTestId('gantt-bar'));
    expect(screen.queryByTestId('gantt-tooltip')).not.toBeInTheDocument();
  });
});

describe('GanttLegend', () => {
  it('shows the approximate badge only for a reconstructed timeline', () => {
    const { rerender } = render(<GanttLegend approximate={false} />);
    expect(screen.queryByTestId('gantt-approximate-badge')).not.toBeInTheDocument();

    rerender(<GanttLegend approximate />);
    expect(screen.getByTestId('gantt-approximate-badge')).toBeInTheDocument();
  });
});
