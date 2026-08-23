import type { TerminalChunk } from '@/stores/terminal';

/** Return the first chunk whose sequence number is greater than writtenSeq. */
export function lowerBoundSeq(chunks: TerminalChunk[], writtenSeq: number): number {
  let lo = 0;
  let hi = chunks.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    const chunk = chunks[mid];
    if (chunk && chunk.seq <= writtenSeq) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}
