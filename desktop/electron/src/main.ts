import { app, BrowserWindow } from 'electron';
import { initLogger } from './logger.js';
import { StAgentDesktopApp, resolvePreloadPath } from './app/StAgent-desktop-app.js';

let desktopApp: StAgentDesktopApp | null = null;

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const win = desktopApp?.getMainWindow();
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
}

app.whenReady().then(async () => {
  initLogger();
  desktopApp = new StAgentDesktopApp(resolvePreloadPath());
  await desktopApp.start();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    desktopApp?.shutdown();
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0 && desktopApp) {
    void desktopApp.reopenMainWindow();
  }
});

app.on('before-quit', () => {
  desktopApp?.shutdown();
});
