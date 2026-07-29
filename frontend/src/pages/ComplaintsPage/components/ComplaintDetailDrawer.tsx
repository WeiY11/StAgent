import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Pencil, Trash2 } from 'lucide-react';
import { Badge, Button, Sheet, SheetContent, SheetHeader, SheetTitle, Textarea } from '@/components/ui';
import { StatusBadge } from '@/components/common/StatusBadge';
import { formatDate } from '@/lib/utils/utils';
import {
  useComplaint,
  useDeleteComplaint,
  useUpdateComplaint,
  type ComplaintInfo,
} from '@/controllers/API/queries/complaints';
import { COMPLAINT_STATUS_BADGE } from '../statusMaps';

interface ComplaintDetailDrawerProps {
  complaint: ComplaintInfo | null;
  onClose: () => void;
  onEdit: (complaint: ComplaintInfo) => void;
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm text-foreground break-words">{value || '—'}</p>
    </div>
  );
}

export function ComplaintDetailDrawer({ complaint: selected, onClose, onEdit }: ComplaintDetailDrawerProps) {
  const { t } = useTranslation();
  const updateMutation = useUpdateComplaint();
  const deleteMutation = useDeleteComplaint();
  const [resolution, setResolution] = useState('');
  // Subscribe to the detail cache so status transitions refresh the open drawer.
  const { data: fresh } = useComplaint(selected?.id ?? '', { enabled: !!selected });
  const complaint = fresh && selected && fresh.id === selected.id ? fresh : selected;

  useEffect(() => {
    setResolution(complaint?.resolution ?? '');
  }, [complaint?.id, complaint?.resolution]);

  if (!complaint) return null;

  const transition = async (status: ComplaintInfo['status']) => {
    await updateMutation.mutateAsync({
      id: complaint.id,
      status,
      ...(status === 'resolved' && resolution.trim()
        ? { resolution: resolution.trim() }
        : {}),
    });
  };

  const remove = async () => {
    if (!window.confirm(t('complaints.detail.confirmDelete'))) return;
    await deleteMutation.mutateAsync(complaint.id);
    onClose();
  };

  return (
    <Sheet open={!!complaint} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="overflow-y-auto sm:max-w-md">
        <SheetHeader>
          <SheetTitle>{t('complaints.detail.title')}</SheetTitle>
        </SheetHeader>

        <div className="mt-4 space-y-4">
          <div className="flex items-start justify-between gap-2">
            <h3 className="text-base font-semibold text-foreground break-words">
              {complaint.title}
            </h3>
            <StatusBadge
              status={COMPLAINT_STATUS_BADGE[complaint.status]}
              customLabel={t(`complaints.status.${complaint.status}`)}
              size="sm"
            />
          </div>

          <div className="flex flex-wrap gap-2">
            <Badge variant="default" size="sm">
              {t(`complaints.category.${complaint.category}`)}
            </Badge>
            <Badge
              variant={
                complaint.severity === 'urgent' || complaint.severity === 'high'
                  ? 'error'
                  : complaint.severity === 'medium'
                    ? 'warning'
                    : 'default'
              }
              size="sm"
            >
              {t(`complaints.severity.${complaint.severity}`)}
            </Badge>
            <Badge variant="default" size="sm">
              {t(`complaints.source.${complaint.source}`)}
            </Badge>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <DetailField
              label={t('complaints.detail.customer')}
              value={complaint.customer_name ?? ''}
            />
            <DetailField
              label={t('complaints.detail.contact')}
              value={complaint.customer_contact ?? ''}
            />
            <DetailField
              label={t('complaints.detail.createdAt')}
              value={formatDate(complaint.created_at)}
            />
            <DetailField
              label={t('complaints.detail.updatedAt')}
              value={formatDate(complaint.updated_at)}
            />
            {complaint.resolved_at && (
              <DetailField
                label={t('complaints.detail.resolvedAt')}
                value={formatDate(complaint.resolved_at)}
              />
            )}
            {complaint.session_id && (
              <DetailField
                label={t('complaints.detail.session')}
                value={complaint.session_id}
              />
            )}
          </div>

          <div>
            <p className="text-xs text-muted-foreground mb-1">
              {t('complaints.detail.description')}
            </p>
            <p className="text-sm text-foreground whitespace-pre-wrap break-words">
              {complaint.description || '—'}
            </p>
          </div>

          {complaint.status !== 'closed' && (
            <div>
              <p className="text-xs text-muted-foreground mb-1">
                {t('complaints.detail.resolution')}
              </p>
              <Textarea
                value={resolution}
                onChange={(e) => setResolution(e.target.value)}
                placeholder={t('complaints.detail.resolutionPlaceholder')}
                rows={3}
              />
            </div>
          )}
          {complaint.status === 'closed' && complaint.resolution && (
            <DetailField
              label={t('complaints.detail.resolution')}
              value={complaint.resolution}
            />
          )}

          <div className="flex flex-wrap gap-2 pt-2 border-t border-border">
            {complaint.status === 'open' && (
              <Button
                size="sm"
                onClick={() => void transition('in_progress')}
                loading={updateMutation.isPending}
              >
                {t('complaints.detail.startProgress')}
              </Button>
            )}
            {(complaint.status === 'open' || complaint.status === 'in_progress') && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void transition('resolved')}
                loading={updateMutation.isPending}
              >
                {t('complaints.detail.markResolved')}
              </Button>
            )}
            {complaint.status === 'resolved' && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void transition('closed')}
                loading={updateMutation.isPending}
              >
                {t('complaints.detail.close')}
              </Button>
            )}
            {complaint.status === 'closed' && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void transition('open')}
                loading={updateMutation.isPending}
              >
                {t('complaints.detail.reopen')}
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              leftIcon={<Pencil className="w-3.5 h-3.5" />}
              onClick={() => onEdit(complaint)}
            >
              {t('complaints.detail.edit')}
            </Button>
            <Button
              size="sm"
              variant="danger"
              leftIcon={<Trash2 className="w-3.5 h-3.5" />}
              onClick={() => void remove()}
              loading={deleteMutation.isPending}
            >
              {t('complaints.detail.delete')}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
