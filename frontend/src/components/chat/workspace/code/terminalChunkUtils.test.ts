import { describe, expect, it } from 'vitest';
import { lowerBoundSeq } from './terminalChunkUtils';

function chunk(seq: number) {
  return {
    tool_call_id: `call-${seq}`,
    stream: 'stdout' as const,
    data: '',
    seq,
    ts: seq,
    tool_name: 'shell_exec',
    source: 'shell',
  };
}

describe('lowerBoundSeq', () => {
  it('returns zero for an empty chunk list', () => {
    expect(lowerBoundSeq([], 0)).toBe(0);
  });

  it('skips chunks already written and returns the first newer chunk', () => {
    const chunks = [chunk(1), chunk(3), chunk(7)];

    expect(lowerBoundSeq(chunks, 3)).toBe(2);
    expect(lowerBoundSeq(chunks, 7)).toBe(3);
  });
});
