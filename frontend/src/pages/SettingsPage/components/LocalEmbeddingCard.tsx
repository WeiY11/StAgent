import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, CardContent, CardHeader, CardTitle, Input, Select } from '@/components/ui';
import { apiClient } from '@/api/client';
import { useAuthStore } from '@/stores/auth';
import { isAdminUser, userHasPermissions } from '@/lib/auth/authUser';
import { useToast } from '@/components/ui/Toaster';

type Model = {
  model_id: string;
  display_name: string;
  dimension: number | null;
  status?: string;
};
type SemanticStatus = {
  status: string;
  model_id: string | null;
  environment_managed: boolean;
  error: string | null;
};

export function LocalEmbeddingCard() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const user = useAuthStore((state) => state.user);
  const authStatus = useAuthStore((state) => state.authStatus);
  const canManage = Boolean(
    authStatus && (!authStatus.enforce_auth || isAdminUser(user) || userHasPermissions(user, 'admin:panel')),
  );
  const [models, setModels] = useState<Model[]>([]);
  const [semantic, setSemantic] = useState<SemanticStatus | null>(null);
  const [sourceDirectory, setSourceDirectory] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [repoId, setRepoId] = useState('');
  const [revision, setRevision] = useState('');
  const [busy, setBusy] = useState(false);
  const reload = useCallback(async () => {
    const statusResult = await apiClient.get<SemanticStatus>('/knowledge/semantic/status');
    setSemantic(statusResult);
    if (canManage) {
      const modelResult = await apiClient.get<{ models: Model[] }>('/knowledge/semantic/models');
      setModels(modelResult.models);
    } else {
      setModels([]);
    }
  }, [canManage]);
  useEffect(() => { void reload().catch(() => undefined); }, [reload]);
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      await reload();
    } catch (error) {
      toast({
        variant: 'error',
        title: t('settings.localEmbeddingOperationFailed'),
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setBusy(false);
    }
  };
  const environmentManaged = Boolean(semantic?.environment_managed);
  const disabled = busy || environmentManaged || !canManage;
  const confirmAndRun = (message: string, action: () => Promise<unknown>) => {
    if (window.confirm(message)) void run(action);
  };
  return <Card><CardHeader><CardTitle>{t('settings.localEmbeddingTitle')}</CardTitle></CardHeader><CardContent className="space-y-3">
    <p className="text-sm text-muted-foreground">{t('settings.localEmbeddingDesc')}</p>
    <p className="text-xs text-muted-foreground">{t('settings.localEmbeddingInstallHint')}</p>
    <p className="text-xs">{t('settings.localEmbeddingStatus', { status: semantic?.status ?? 'off' })}</p>
    {environmentManaged && <p className="text-xs text-muted-foreground">{t('settings.localEmbeddingEnvironmentManaged')}</p>}
    {!canManage && <p className="text-xs text-muted-foreground">{t('settings.localEmbeddingReadOnly')}</p>}
    {canManage && <>
      <Select value={semantic?.model_id ?? ''} disabled={disabled} onChange={(event) => void run(() => apiClient.put('/knowledge/semantic/config', { enabled: true, vector_backend: 'sqlite-vec', local_model_id: event.target.value, auto_reindex: true }))}>
        <option value="">{t('settings.localEmbeddingChoose')}</option>{models.map((model) => <option key={model.model_id} value={model.model_id}>{model.display_name} ({model.dimension ?? '?'})</option>)}
      </Select>
      <div className="flex gap-2"><Input value={sourceDirectory} onChange={(event) => setSourceDirectory(event.target.value)} placeholder={t('settings.localEmbeddingDirectory')} /><Input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder={t('settings.localEmbeddingDisplayName')} /><Button disabled={!sourceDirectory || disabled} onClick={() => void run(() => apiClient.post('/knowledge/semantic/models/import', { source_directory: sourceDirectory, display_name: displayName || 'local-model' }))}>{t('settings.localEmbeddingImport')}</Button></div>
      <div className="flex gap-2"><Input value={repoId} onChange={(event) => setRepoId(event.target.value)} placeholder={t('settings.localEmbeddingRepo')} /><Input value={revision} onChange={(event) => setRevision(event.target.value)} placeholder={t('settings.localEmbeddingRevision')} /><Button disabled={!repoId || disabled} onClick={() => void run(() => apiClient.post('/knowledge/semantic/models/download', { repo_id: repoId, revision: revision || undefined, display_name: displayName || undefined }))}>{t('settings.localEmbeddingDownload')}</Button><Button variant="secondary" disabled={disabled} onClick={() => void run(() => apiClient.post('/knowledge/semantic/rebuild', {}))}>{t('settings.localEmbeddingRebuild')}</Button></div>
      <div className="space-y-1">
        {models.map((model) => <div className="flex items-center justify-between gap-2 text-xs" key={model.model_id}><span>{model.display_name} ({model.dimension ?? '?'})</span><Button size="sm" variant="ghost" disabled={disabled || semantic?.model_id === model.model_id} onClick={() => confirmAndRun(t('settings.localEmbeddingDeleteConfirm'), () => apiClient.delete(`/knowledge/semantic/models/${encodeURIComponent(model.model_id)}`))}>{t('settings.localEmbeddingDelete')}</Button></div>)}
      </div>
      <Button variant="ghost" disabled={disabled || semantic?.status === 'off'} onClick={() => confirmAndRun(t('settings.localEmbeddingDisableConfirm'), () => apiClient.put('/knowledge/semantic/config', { enabled: false, vector_backend: 'sqlite-vec', local_model_id: semantic?.model_id ?? '', auto_reindex: false }))}>{t('settings.localEmbeddingDisable')}</Button>
    </>}
  </CardContent></Card>;
}
