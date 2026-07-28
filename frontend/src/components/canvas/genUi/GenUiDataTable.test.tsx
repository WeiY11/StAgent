import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { GenUiNode } from '@/types/genUi';
import {
  renderDataTable,
  DATA_TABLE_MAX_COLS,
  DATA_TABLE_MAX_ROWS,
} from './proComponents';

function renderNode(props?: Record<string, unknown>): ReturnType<typeof render> {
  const node: GenUiNode = { nodeId: 'dt1', kind: 'DataTable', props };
  const el: ReactNode = renderDataTable({ node, depth: 0, renderChild: () => null });
  return render(<>{el}</>);
}

describe('renderDataTable', () => {
  it('renders title, headers and rows', () => {
    renderNode({
      title: 'Quarterly revenue',
      columns: ['Region', 'Q1'],
      rows: [
        ['EMEA', 120],
        ['APAC', 98],
      ],
    });
    expect(screen.getByText('Quarterly revenue')).toBeInTheDocument();
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('Region')).toBeInTheDocument();
    expect(screen.getByText('EMEA')).toBeInTheDocument();
    expect(screen.getByText('98')).toBeInTheDocument();
    expect(screen.getAllByRole('row')).toHaveLength(3); // header + 2 data rows
  });

  it('truncates rows past the limit and shows a badge', () => {
    const rows = Array.from({ length: DATA_TABLE_MAX_ROWS + 25 }, (_, i) => [`row-${i}`]);
    renderNode({ columns: ['Name'], rows });
    expect(
      screen.getByText(`showing ${DATA_TABLE_MAX_ROWS} of ${DATA_TABLE_MAX_ROWS + 25} rows`),
    ).toBeInTheDocument();
    expect(screen.getByText(`row-${DATA_TABLE_MAX_ROWS - 1}`)).toBeInTheDocument();
    expect(screen.queryByText(`row-${DATA_TABLE_MAX_ROWS}`)).not.toBeInTheDocument();
  });

  it('truncates columns past the limit and shows a badge', () => {
    const wide = DATA_TABLE_MAX_COLS + 5;
    const columns = Array.from({ length: wide }, (_, i) => `col-${i}`);
    renderNode({ columns, rows: [Array.from({ length: wide }, (_, i) => i)] });
    expect(
      screen.getByText(`showing ${DATA_TABLE_MAX_COLS} of ${wide} columns`),
    ).toBeInTheDocument();
    expect(screen.getByText(`col-${DATA_TABLE_MAX_COLS - 1}`)).toBeInTheDocument();
    expect(screen.queryByText(`col-${DATA_TABLE_MAX_COLS}`)).not.toBeInTheDocument();
  });

  it('degrades to an empty state when props are missing', () => {
    renderNode(undefined);
    expect(screen.getByText(/No table data/i)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('tolerates malformed rows and non-string cells without throwing', () => {
    renderNode({
      columns: ['A', 'B'],
      rows: ['not-an-array', ['x', { nested: true }], [null, undefined], 42],
    });
    // Only the two array-shaped rows survive; object cells are stringified.
    expect(screen.getAllByRole('row')).toHaveLength(3); // header + 2 valid rows
    expect(screen.getByText('x')).toBeInTheDocument();
    expect(screen.getByText('{"nested":true}')).toBeInTheDocument();
    expect(screen.queryByText('not-an-array')).not.toBeInTheDocument();
  });
});
