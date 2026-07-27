import { useDesktop } from '@/hooks/useDesktop';

/**
 * Invisible top drag strip for desktop screens rendered outside AppShell
 * (login/setup, suspense fallback), where no TitleBar drag region exists.
 * Renders nothing in the browser.
 */
export function DesktopDragRegion() {
  const { isDesktop } = useDesktop();
  if (!isDesktop) return null;
  return (
    <div
      aria-hidden
      className="fixed inset-x-0 top-0 z-50 h-9 [-webkit-app-region:drag] [app-region:drag]"
    />
  );
}
