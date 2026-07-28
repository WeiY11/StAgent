import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DataFileSuggestionChips } from '@/components/chat/composer/DataFileSuggestionChips';
import { useChatDraftStore } from '@/stores/chatDraft';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { defaultValue?: string }) => opts?.defaultValue ?? key,
  }),
}));

const csvFile = () => new File(['a,b\n1,2'], 'sales.csv', { type: 'text/csv' });
const xlsxFile = () =>
  new File(['bin'], 'report.XLSX', {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
const pngFile = () => new File(['img'], 'photo.png', { type: 'image/png' });

describe('DataFileSuggestionChips', () => {
  beforeEach(() => {
    useChatDraftStore.setState({ pendingInsertQueue: [], insertCounter: 0 });
  });

  it('renders nothing without a CSV/Excel attachment', () => {
    const { container } = render(<DataFileSuggestionChips files={[pngFile()]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the three suggestion chips when a data file is staged', () => {
    render(<DataFileSuggestionChips files={[pngFile(), csvFile()]} />);
    expect(screen.getByText('chat.dataUpload.chips.overview.label')).toBeInTheDocument();
    expect(screen.getByText('chat.dataUpload.chips.clean.label')).toBeInTheDocument();
    expect(screen.getByText('chat.dataUpload.chips.chart.label')).toBeInTheDocument();
  });

  it('matches extensions case-insensitively (.XLSX)', () => {
    render(<DataFileSuggestionChips files={[xlsxFile()]} />);
    expect(screen.getByText('chat.dataUpload.chips.overview.label')).toBeInTheDocument();
  });

  it('queues the prompt into the composer draft on chip click (no auto-send)', () => {
    render(<DataFileSuggestionChips files={[csvFile()]} />);
    fireEvent.click(screen.getByText('chat.dataUpload.chips.overview.label'));
    expect(useChatDraftStore.getState().pendingInsertQueue).toEqual([
      'chat.dataUpload.chips.overview.prompt',
    ]);
  });

  it('hides after dismiss and re-arms once data files are removed', () => {
    const { rerender } = render(<DataFileSuggestionChips files={[csvFile()]} />);
    fireEvent.click(
      screen.getByRole('button', { name: 'Dismiss data suggestions' }),
    );
    expect(screen.queryByText('chat.dataUpload.chips.overview.label')).toBeNull();

    // Still hidden while the data file stays attached.
    rerender(<DataFileSuggestionChips files={[csvFile()]} />);
    expect(screen.queryByText('chat.dataUpload.chips.overview.label')).toBeNull();

    // Removing all attachments resets the dismissal; a new data file shows chips again.
    rerender(<DataFileSuggestionChips files={[]} />);
    rerender(<DataFileSuggestionChips files={[csvFile()]} />);
    expect(screen.getByText('chat.dataUpload.chips.overview.label')).toBeInTheDocument();
  });

  it('disappears when the attachment list no longer has data files', () => {
    const { rerender, container } = render(
      <DataFileSuggestionChips files={[csvFile()]} />,
    );
    rerender(<DataFileSuggestionChips files={[pngFile()]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
