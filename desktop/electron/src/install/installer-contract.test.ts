import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

describe('NSIS installation-directory contract', () => {
  it('keeps an existing per-user or per-machine install location during upgrades', () => {
    const installer = fs.readFileSync(
      path.join(process.cwd(), 'build', 'installer.nsh'),
      'utf8',
    );

    expect(installer).toContain('${if} $hasPerUserInstallation == "0"');
    expect(installer).toContain('${andif} $hasPerMachineInstallation == "0"');
    expect(installer).toContain('StrCpy $INSTDIR "$LOCALAPPDATA\\LeAgent"');
  });

  it('does not overwrite an explicit /D install directory in unattended installs', () => {
    const installer = fs.readFileSync(
      path.join(process.cwd(), 'build', 'installer.nsh'),
      'utf8',
    );

    expect(installer).toContain('${GetParameters} $R0');
    expect(installer).toContain('${GetOptions} $R0 "/D=" $R1');
    expect(installer).toContain('${If} ${Errors}');
  });
});
