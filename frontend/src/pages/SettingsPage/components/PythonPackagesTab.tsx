import { useState, useCallback, useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Terminal,
  RefreshCw,
  Trash2,
  Search,
  ArrowUpCircle,
  GitBranch,
} from 'lucide-react';
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  Button,
  Input,
  Modal,
  ModalHeader,
  ModalBody,
  ModalFooter,
  Badge,
} from '@/components/ui';
import { PageLoader } from '@/components/common/PageLoader';
import { apiClient } from '@/api/client';
import { useToast } from '@/components/ui/Toaster';
import type { PythonPackageRow, PythonEnvInfoResponse, PythonFilterMode } from '../constants';

export function PythonPackagesTab() {
  const { t } = useTranslation();
  const { toast } = useToast();

  const [pythonEnvInfo, setPythonEnvInfo] = useState<PythonEnvInfoResponse | null>(null);
  const [pythonPackages, setPythonPackages] = useState<PythonPackageRow[]>([]);
  const [pythonPackagesLoading, setPythonPackagesLoading] = useState(false);
  const [pythonInstallSpec, setPythonInstallSpec] = useState('');
  const [pythonEnvBusyId, setPythonEnvBusyId] = useState<string | null>(null);
  const [pythonSearch, setPythonSearch] = useState('');
  const [pythonFilter, setPythonFilter] = useState<PythonFilterMode>('all');
  const [pythonOutdated, setPythonOutdated] = useState<Record<string, string>>({});
  const [pythonOutdatedLoading, setPythonOutdatedLoading] = useState(false);
  const [pythonLogText, setPythonLogText] = useState<string | null>(null);
  const [pythonTreeText, setPythonTreeText] = useState<string | null>(null);
  const [pythonConfirmUninstall, setPythonConfirmUninstall] = useState<string | null>(null);

  const loadPythonEnvironment = useCallback(async () => {
    setPythonPackagesLoading(true);
    try {
      const [infoRes, pkgRes] = await Promise.all([
        apiClient.get<PythonEnvInfoResponse>('/python-env/info'),
        apiClient.get<{ packages: PythonPackageRow[] }>('/python-env/packages'),
      ]);
      setPythonEnvInfo(infoRes);
      setPythonPackages(pkgRes.packages);
    } catch (e) {
      setPythonEnvInfo(null);
      setPythonPackages([]);
      toast({
        variant: 'error',
        title: e instanceof Error ? e.message : t('settings.pythonPackagesEmpty'),
      });
    } finally {
      setPythonPackagesLoading(false);
    }
  }, [t, toast]);

  useEffect(() => {
    void loadPythonEnvironment();
  }, [loadPythonEnvironment]);

  const handlePythonInstall = useCallback(async () => {
    const spec = pythonInstallSpec.trim();
    if (!spec) return;
    setPythonEnvBusyId('install');
    try {
      const res = await apiClient.post<{ ok: boolean; log: string }>('/python-env/install', { spec });
      toast({ variant: 'success', title: t('settings.pythonPackagesInstall') });
      setPythonInstallSpec('');
      if (res.log) setPythonLogText(res.log);
      await loadPythonEnvironment();
    } catch (e) {
      toast({ variant: 'error', title: e instanceof Error ? e.message : 'Error' });
    } finally {
      setPythonEnvBusyId(null);
    }
  }, [pythonInstallSpec, loadPythonEnvironment, t, toast]);

  const handlePythonUninstall = useCallback(
    async (pkgName: string) => {
      setPythonEnvBusyId(pkgName);
      setPythonConfirmUninstall(null);
      try {
        const res = await apiClient.post<{ ok: boolean; log: string }>('/python-env/uninstall', { package: pkgName });
        toast({ variant: 'success', title: t('settings.pythonPackagesUninstall') });
        if (res.log) setPythonLogText(res.log);
        await loadPythonEnvironment();
      } catch (e) {
        toast({ variant: 'error', title: e instanceof Error ? e.message : 'Error' });
      } finally {
        setPythonEnvBusyId(null);
      }
    },
    [loadPythonEnvironment, t, toast],
  );

  const handlePythonUpgrade = useCallback(
    async (pkgName: string) => {
      setPythonEnvBusyId(`upgrade:${pkgName}`);
      try {
        const res = await apiClient.post<{ ok: boolean; log: string }>('/python-env/upgrade', { package: pkgName });
        toast({ variant: 'success', title: t('settings.pythonPackagesUpgradeSuccess') });
        if (res.log) setPythonLogText(res.log);
        setPythonOutdated((prev) => {
          const next = { ...prev };
          delete next[pkgName];
          return next;
        });
        await loadPythonEnvironment();
      } catch (e) {
        toast({ variant: 'error', title: e instanceof Error ? e.message : 'Error' });
      } finally {
        setPythonEnvBusyId(null);
      }
    },
    [loadPythonEnvironment, t, toast],
  );

  const handleCheckOutdated = useCallback(async () => {
    setPythonOutdatedLoading(true);
    try {
      const res = await apiClient.get<{ packages: Array<{ name: string; latest_version: string }> }>('/python-env/outdated');
      const map: Record<string, string> = {};
      for (const pkg of res.packages) {
        map[pkg.name] = pkg.latest_version;
      }
      setPythonOutdated(map);
      if (res.packages.length === 0) {
        toast({ variant: 'success', title: t('settings.pythonPackagesNoOutdated') });
      }
    } catch (e) {
      toast({ variant: 'error', title: e instanceof Error ? e.message : 'Error' });
    } finally {
      setPythonOutdatedLoading(false);
    }
  }, [t, toast]);

  const handlePythonSync = useCallback(async () => {
    setPythonEnvBusyId('sync');
    try {
      const res = await apiClient.post<{ ok: boolean; log: string }>('/python-env/sync');
      toast({ variant: 'success', title: t('settings.pythonPackagesSyncSuccess') });
      if (res.log) setPythonLogText(res.log);
      await loadPythonEnvironment();
    } catch (e) {
      toast({ variant: 'error', title: e instanceof Error ? e.message : 'Error' });
    } finally {
      setPythonEnvBusyId(null);
    }
  }, [loadPythonEnvironment, t, toast]);

  const handlePythonTree = useCallback(async () => {
    setPythonEnvBusyId('tree');
    try {
      const res = await apiClient.get<{ tree: string }>('/python-env/tree');
      setPythonTreeText(res.tree);
    } catch (e) {
      toast({ variant: 'error', title: e instanceof Error ? e.message : 'Error' });
    } finally {
      setPythonEnvBusyId(null);
    }
  }, [toast]);

  const filteredPythonPackages = useMemo(() => {
    let list = pythonPackages;
    if (pythonFilter === 'direct') {
      list = list.filter((p) => p.is_direct);
    } else if (pythonFilter === 'outdated') {
      list = list.filter((p) => p.name in pythonOutdated);
    }
    const q = pythonSearch.trim().toLowerCase();
    if (q) {
      list = list.filter((p) => p.name.toLowerCase().includes(q));
    }
    return [...list].sort((a, b) => a.name.localeCompare(b.name));
  }, [pythonPackages, pythonSearch, pythonFilter, pythonOutdated]);

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>{t('settings.pythonPackagesTitle')}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">{t('settings.pythonPackagesDesc')}</p>

          {/* env info banner */}
          {pythonEnvInfo ? (
            <div className="rounded-lg border border-border bg-muted/10 p-3 space-y-1.5">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                  <Terminal className="h-3.5 w-3.5 shrink-0 opacity-70" aria-hidden />
                  <span className="font-medium text-foreground/80">{t('settings.pythonPackagesInterpreter')}:</span>
                  <code className="font-mono text-[11px] break-all">{pythonEnvInfo.python_executable}</code>
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {pythonEnvInfo.project_mode ? (
                  <Badge variant="default" className="text-[10px] px-1.5 py-0">
                    {t('settings.pythonPackagesUvProject')}
                  </Badge>
                ) : pythonEnvInfo.uses_uv ? (
                  <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
                    {t('settings.pythonPackagesUv')}
                  </Badge>
                ) : (
                  <Badge variant="secondary" className="text-[10px] px-1.5 py-0">pip</Badge>
                )}
                {pythonEnvInfo.has_lockfile && (
                  <Badge variant="outline" className="text-[10px] px-1.5 py-0">uv.lock</Badge>
                )}
                {pythonPackages.length > 0 && (
                  <span className="text-[11px] text-muted-foreground tabular-nums">
                    {t('settings.pythonPackagesTotal', { count: pythonPackages.length })}
                  </span>
                )}
              </div>
            </div>
          ) : null}

          {/* install bar */}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <div className="flex-1 min-w-0">
              <Input
                value={pythonInstallSpec}
                onChange={(e) => setPythonInstallSpec(e.target.value)}
                placeholder={t('settings.pythonPackagesPlaceholder')}
                autoComplete="off"
                aria-label={t('settings.pythonPackagesPlaceholder')}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void handlePythonInstall();
                }}
              />
            </div>
            <div className="flex flex-wrap gap-1.5 sm:shrink-0">
              <Button
                type="button"
                size="sm"
                onClick={() => void handlePythonInstall()}
                disabled={pythonPackagesLoading || pythonEnvBusyId !== null || !pythonInstallSpec.trim()}
              >
                {pythonEnvBusyId === 'install' ? '…' : t('settings.pythonPackagesInstall')}
              </Button>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => void loadPythonEnvironment()}
                disabled={pythonPackagesLoading || pythonEnvBusyId !== null}
                leftIcon={<RefreshCw className="h-3.5 w-3.5 shrink-0" aria-hidden />}
              >
                {t('settings.pythonPackagesRefresh')}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void handleCheckOutdated()}
                disabled={pythonPackagesLoading || pythonEnvBusyId !== null || pythonOutdatedLoading}
                loading={pythonOutdatedLoading}
              >
                {t('settings.pythonPackagesCheckOutdated')}
              </Button>
              {pythonEnvInfo?.project_mode && (
                <>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void handlePythonSync()}
                    disabled={pythonPackagesLoading || pythonEnvBusyId !== null}
                    loading={pythonEnvBusyId === 'sync'}
                  >
                    {t('settings.pythonPackagesSync')}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void handlePythonTree()}
                    disabled={pythonPackagesLoading || pythonEnvBusyId !== null}
                    leftIcon={<GitBranch className="h-3.5 w-3.5 shrink-0" aria-hidden />}
                    loading={pythonEnvBusyId === 'tree'}
                  >
                    {t('settings.pythonPackagesTree')}
                  </Button>
                </>
              )}
            </div>
          </div>

          {/* search + filter bar */}
          {!pythonPackagesLoading && pythonPackages.length > 0 && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <div className="relative flex-1 min-w-0">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
                <Input
                  value={pythonSearch}
                  onChange={(e) => setPythonSearch(e.target.value)}
                  placeholder={t('settings.pythonPackagesSearch')}
                  className="pl-8 h-8 text-xs"
                  autoComplete="off"
                />
              </div>
              <div className="flex gap-1 shrink-0">
                {(['all', 'direct', 'outdated'] as const).map((mode) => (
                  <Button
                    key={mode}
                    type="button"
                    variant={pythonFilter === mode ? 'primary' : 'ghost'}
                    size="sm"
                    className="h-8 px-2.5 text-xs"
                    onClick={() => setPythonFilter(mode)}
                  >
                    {mode === 'all'
                      ? t('settings.pythonPackagesShowAll')
                      : mode === 'direct'
                        ? t('settings.pythonPackagesShowDirect')
                        : t('settings.pythonPackagesShowOutdated')}
                    {mode === 'outdated' && Object.keys(pythonOutdated).length > 0 && (
                      <Badge variant="default" className="ml-1 text-[9px] px-1 py-0 min-w-[1.1rem] justify-center">
                        {Object.keys(pythonOutdated).length}
                      </Badge>
                    )}
                  </Button>
                ))}
              </div>
            </div>
          )}

          {/* package list */}
          {pythonPackagesLoading ? (
            <div className="flex justify-center py-8">
              <PageLoader size="sm" message={t('settings.pythonPackagesLoading')} />
            </div>
          ) : pythonPackages.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t('settings.pythonPackagesEmpty')}</p>
          ) : (
            <ul className="max-h-[min(520px,55vh)] overflow-y-auto rounded-lg border border-border divide-y divide-border bg-muted/15">
              {filteredPythonPackages.map((pkg) => {
                const outdatedVer = pythonOutdated[pkg.name];
                return (
                  <li
                    key={pkg.name}
                    className="group flex items-center justify-between gap-2 px-3 py-1.5 hover:bg-muted/30 transition-colors"
                  >
                    <div className="min-w-0 flex items-center gap-2">
                      <span className="truncate text-xs font-medium text-foreground">{pkg.name}</span>
                      <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
                        {pkg.version}
                      </span>
                      {pkg.is_direct && (
                        <Badge variant="outline" className="text-[9px] px-1 py-0 opacity-70">
                          {t('settings.pythonPackagesDirect')}
                        </Badge>
                      )}
                      {outdatedVer && (
                        <Badge variant="secondary" className="text-[9px] px-1 py-0 text-amber-600 dark:text-amber-400 border-amber-300 dark:border-amber-700">
                          {t('settings.pythonPackagesLatest', { version: outdatedVer })}
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                      {outdatedVer && (
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          className="h-7 w-7 rounded-md p-0 text-muted-foreground hover:bg-primary/10 hover:text-primary"
                          loading={pythonEnvBusyId === `upgrade:${pkg.name}`}
                          leftIcon={<ArrowUpCircle className="h-3.5 w-3.5" aria-hidden />}
                          aria-label={t('settings.pythonPackagesUpgrade')}
                          title={t('settings.pythonPackagesUpgrade')}
                          onClick={() => void handlePythonUpgrade(pkg.name)}
                          disabled={pythonEnvBusyId !== null}
                        />
                      )}
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        className="h-7 w-7 rounded-md p-0 text-muted-foreground hover:bg-red-500/10 hover:text-red-600 dark:hover:text-red-400"
                        loading={pythonEnvBusyId === pkg.name}
                        leftIcon={<Trash2 className="h-3.5 w-3.5" aria-hidden />}
                        aria-label={t('settings.pythonPackagesUninstall')}
                        title={t('settings.pythonPackagesUninstall')}
                        onClick={() => setPythonConfirmUninstall(pkg.name)}
                        disabled={pythonEnvBusyId !== null}
                      />
                    </div>
                  </li>
                );
              })}
              {filteredPythonPackages.length === 0 && (
                <li className="px-3 py-6 text-center text-xs text-muted-foreground">
                  {t('settings.pythonPackagesEmpty')}
                </li>
              )}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* confirm uninstall modal */}
      <Modal isOpen={pythonConfirmUninstall !== null} onClose={() => setPythonConfirmUninstall(null)} size="sm">
        <ModalHeader onClose={() => setPythonConfirmUninstall(null)}>
          {t('settings.pythonPackagesUninstall')}
        </ModalHeader>
        <ModalBody>
          <p className="text-sm">
            {t('settings.pythonPackagesConfirmUninstall', { name: pythonConfirmUninstall ?? '' })}
          </p>
        </ModalBody>
        <ModalFooter>
          <Button variant="secondary" size="sm" onClick={() => setPythonConfirmUninstall(null)}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={() => pythonConfirmUninstall && void handlePythonUninstall(pythonConfirmUninstall)}
            loading={pythonEnvBusyId === pythonConfirmUninstall}
          >
            {t('settings.pythonPackagesUninstall')}
          </Button>
        </ModalFooter>
      </Modal>

      {/* log modal */}
      <Modal isOpen={pythonLogText !== null} onClose={() => setPythonLogText(null)} size="lg">
        <ModalHeader onClose={() => setPythonLogText(null)}>
          {t('settings.pythonPackagesLog')}
        </ModalHeader>
        <ModalBody>
          <pre className="max-h-[60vh] overflow-auto rounded-lg bg-muted/30 p-4 text-xs font-mono whitespace-pre-wrap break-all">
            {pythonLogText}
          </pre>
        </ModalBody>
      </Modal>

      {/* dep tree modal */}
      <Modal isOpen={pythonTreeText !== null} onClose={() => setPythonTreeText(null)} size="lg">
        <ModalHeader onClose={() => setPythonTreeText(null)}>
          {t('settings.pythonPackagesTreeTitle')}
        </ModalHeader>
        <ModalBody>
          <pre className="max-h-[60vh] overflow-auto rounded-lg bg-muted/30 p-4 text-xs font-mono whitespace-pre-wrap break-all">
            {pythonTreeText}
          </pre>
        </ModalBody>
      </Modal>
    </>
  );
}
