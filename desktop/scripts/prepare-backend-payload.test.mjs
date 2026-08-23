import assert from 'node:assert/strict';
import test from 'node:test';
import { getIncludeEntries, resolveBackendPackage } from './prepare-backend-payload.mjs';

test('payload stages the backend package used by the desktop runtime', () => {
  assert.equal(resolveBackendPackage(), 'StAgent');
  assert.deepEqual(getIncludeEntries()[0], 'StAgent');
  assert.equal(getIncludeEntries().includes('leagent'), false);
});
