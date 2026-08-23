import { useMemo } from 'react';

export interface DesktopInfo {
  /** Running inside the Electron desktop shell (bridge present). */
  isDesktop: boolean;
  /** Platform reported by the shell, or null in a browser. */
  platform: NodeJS.Platform | null;
  /** How window controls should be rendered. */
  titleBarStyle: StAgentTitleBarStyle | null;
  /** The desktop bridge, or null in a browser. */
  bridge: StAgentDesktopBridge | null;
}

/**
 * Detect the Electron desktop shell and expose its window/title-bar capabilities.
 * Returns inert values in the browser so callers can render unconditionally.
 */
export function useDesktop(): DesktopInfo {
  return useMemo(() => {
    const bridge = typeof window !== 'undefined' ? window.StAgent ?? null : null;
    return {
      isDesktop: Boolean(bridge),
      platform: bridge?.platform ?? null,
      titleBarStyle: bridge?.window?.style ?? null,
      bridge,
    };
  }, []);
}
