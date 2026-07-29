import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  CheckCircle2,
  Inbox,
  ListTodo,
  Loader2,
  MessageSquareWarning,
  Plus,
} from 'lucide-react';
import { PageShell } from '@/components/layout/PageShell';
import { StatsCard } from '@/components/common/StatsCard';
import { StatusBadge } from '@/components/common/StatusBadge';
import { DataTable, type Column } from '@/components/common/DataTable';
import { Badge, Button, Input, Select } from '@/components/ui';
import { formatDate } from '@/lib/utils/utils';
import {
  useComplaints,
  useComplaintStats,
  type ComplaintCategory,
  type ComplaintInfo,
  type ComplaintSeverity,
  type ComplaintStatus,
} from '@/controllers/API/queries/complaints';
import { ComplaintModal } from './components/ComplaintModal';
import { ComplaintDetailDrawer } from './components/ComplaintDetailDrawer';
import { COMPLAINT_STATUS_BADGE } from './statusMaps';

const STATUSES: ComplaintStatus[] = ['open', 'in_progress', 'resolved', 'closed'];
const CATEGORIES: ComplaintCategory[] = ['product', 'service', 'billing', 'logistics', 'other'];
const SEVERITIES: ComplaintSeverity[] = ['low', 'medium', 'high', 'urgent'];

const SEVERITY_BADGE_VARIANT: Record<
  ComplaintSeverity,
  'default' | 'warning' | 'error'
> = {
  low: 'default',
  medium: 'warning',
  high: 'error',
  urgent: 'error',
};

export default function ComplaintsPage() {
  const { t } = useTranslation();

  const [statusFilter, setStatusFilter] = useState<ComplaintStatus | 'all'>('all');
  const [categoryFilter, setCategoryFilter] = useState<ComplaintCategory | 'all'>('all');
  const [severityFilter, setSeverityFilter] = useState<ComplaintSeverity | 'all'>('all');
  const [search, setSearch] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ComplaintInfo | null>(null);
  const [selected, setSelected] = useState<ComplaintInfo | null>(null);

  const filters = useMemo(
    () => ({
      status: statusFilter === 'all' ? undefined : statusFilter,
      category: categoryFilter === 'all' ? undefined : categoryFilter,
      severity: severityFilter === 'all' ? undefined : severityFilter,
    }),
    [statusFilter, categoryFilter, severityFilter],
  );

  const { data: complaints, isLoading } = useComplaints(filters);
  const { data: stats } = useComplaintStats();

  const visibleComplaints = useMemo(() => {
    const list = complaints ?? [];
    const query = search.trim().toLowerCase();
    if (!query) return list;
    return list.filter(
      (c) =>
        c.title.toLowerCase().includes(query) ||
        (c.customer_name ?? '').toLowerCase().includes(query),
    );
  }, [complaints, search]);

  const openCreate = () => {
    setEditing(null);
    setModalOpen(true);
  };

  const openEdit = (complaint: ComplaintInfo) => {
    setSelected(null);
    setEditing(complaint);
    setModalOpen(true);
  };

  const columns = useMemo<Column<ComplaintInfo>[]>(
    () => [
      {
        id: 'title',
        header: t('complaints.table.title'),
        cell: ({ row }) => (
          <span className="font-medium text-foreground line-clamp-1">{row.title}</span>
        ),
        minWidth: 200,
      },
      {
        id: 'customer',
        header: t('complaints.table.customer'),
        cell: ({ row }) => row.customer_name || '—',
      },
      {
        id: 'category',
        header: t('complaints.table.category'),
        cell: ({ row }) => (
          <Badge variant="default" size="sm">
            {t(`complaints.category.${row.category}`)}
          </Badge>
        ),
      },
      {
        id: 'severity',
        header: t('complaints.table.severity'),
        cell: ({ row }) => (
          <Badge variant={SEVERITY_BADGE_VARIANT[row.severity]} size="sm">
            {t(`complaints.severity.${row.severity}`)}
          </Badge>
        ),
      },
      {
        id: 'status',
        header: t('complaints.table.status'),
        cell: ({ row }) => (
          <StatusBadge
            status={COMPLAINT_STATUS_BADGE[row.status]}
            customLabel={t(`complaints.status.${row.status}`)}
            size="sm"
          />
        ),
      },
      {
        id: 'source',
        header: t('complaints.table.source'),
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground">
            {t(`complaints.source.${row.source}`)}
          </span>
        ),
      },
      {
        id: 'created_at',
        header: t('complaints.table.createdAt'),
        cell: ({ row }) => (
          <span className="text-xs text-muted-foreground whitespace-nowrap">
            {formatDate(row.created_at)}
          </span>
        ),
      },
    ],
    [t],
  );

  return (
    <PageShell
      icon={<MessageSquareWarning className="w-5 h-5" aria-hidden />}
      title={t('complaints.title')}
      description={t('complaints.description')}
      actions={
        <Button leftIcon={<Plus className="w-4 h-4" />} onClick={openCreate}>
          {t('complaints.newComplaint')}
        </Button>
      }
    >
      <div className="space-y-6">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatsCard
            title={t('complaints.stats.open')}
            value={stats?.by_status.open ?? 0}
            icon={<Inbox className="w-5 h-5" />}
            color="yellow"
            onClick={() => setStatusFilter('open')}
          />
          <StatsCard
            title={t('complaints.stats.inProgress')}
            value={stats?.by_status.in_progress ?? 0}
            icon={<Loader2 className="w-5 h-5" />}
            color="blue"
            onClick={() => setStatusFilter('in_progress')}
          />
          <StatsCard
            title={t('complaints.stats.resolved')}
            value={stats?.by_status.resolved ?? 0}
            icon={<CheckCircle2 className="w-5 h-5" />}
            color="green"
            onClick={() => setStatusFilter('resolved')}
          />
          <StatsCard
            title={t('complaints.stats.total')}
            value={stats?.total ?? 0}
            icon={<ListTodo className="w-5 h-5" />}
            color="gray"
            onClick={() => setStatusFilter('all')}
          />
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
          <Select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as ComplaintStatus | 'all')}
          >
            <option value="all">{t('complaints.filters.allStatuses')}</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`complaints.status.${s}`)}
              </option>
            ))}
          </Select>
          <Select
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value as ComplaintCategory | 'all')}
          >
            <option value="all">{t('complaints.filters.allCategories')}</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {t(`complaints.category.${c}`)}
              </option>
            ))}
          </Select>
          <Select
            value={severityFilter}
            onChange={(e) => setSeverityFilter(e.target.value as ComplaintSeverity | 'all')}
          >
            <option value="all">{t('complaints.filters.allSeverities')}</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {t(`complaints.severity.${s}`)}
              </option>
            ))}
          </Select>
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('complaints.filters.searchPlaceholder')}
          />
        </div>

        <DataTable<ComplaintInfo>
          data={visibleComplaints}
          columns={columns}
          loading={isLoading}
          emptyMessage={t('complaints.empty.title')}
          emptyAction={{ label: t('complaints.newComplaint'), onClick: openCreate }}
          rowKey={(row) => row.id}
          onRowClick={(row) => setSelected(row)}
        />
      </div>

      <ComplaintModal
        isOpen={modalOpen}
        onClose={() => {
          setModalOpen(false);
          setEditing(null);
        }}
        complaint={editing}
      />
      <ComplaintDetailDrawer
        complaint={selected}
        onClose={() => setSelected(null)}
        onEdit={openEdit}
      />
    </PageShell>
  );
}
