/**
 * Canonical StAgent entrypoint kept separate from the historical filename so
 * packaged imports and source-tree acceptance checks resolve the same module.
 */
export {
  StAgentDesktopApp,
  resolvePreloadPath,
} from './leagent-desktop-app.js';
