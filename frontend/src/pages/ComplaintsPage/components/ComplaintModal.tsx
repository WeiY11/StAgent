import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Modal, ModalBody, ModalFooter, ModalHeader, Select, Textarea } from '@/components/ui';
import {
  useCreateComplaint,
  useUpdateComplaint,
  type ComplaintCategory,
  type ComplaintInfo,
  type ComplaintSeverity,
} from '@/controllers/API/queries/complaints';

const CATEGORIES: ComplaintCategory[] = ['product', 'service', 'billing', 'logistics', 'other'];
const SEVERITIES: ComplaintSeverity[] = ['low', 'medium', 'high', 'urgent'];

interface ComplaintModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** When set, the modal edits this complaint instead of creating a new one. */
  complaint?: ComplaintInfo | null;
}

export function ComplaintModal({ isOpen, onClose, complaint }: ComplaintModalProps) {
  const { t } = useTranslation();
  const createMutation = useCreateComplaint();
  const updateMutation = useUpdateComplaint();

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [customerName, setCustomerName] = useState('');
  const [customerContact, setCustomerContact] = useState('');
  const [category, setCategory] = useState<ComplaintCategory>('other');
  const [severity, setSeverity] = useState<ComplaintSeverity>('medium');

  const isEdit = !!complaint;
  const pending = createMutation.isPending || updateMutation.isPending;

  useEffect(() => {
    if (!isOpen) return;
    setTitle(complaint?.title ?? '');
    setDescription(complaint?.description ?? '');
    setCustomerName(complaint?.customer_name ?? '');
    setCustomerContact(complaint?.customer_contact ?? '');
    setCategory(complaint?.category ?? 'other');
    setSeverity(complaint?.severity ?? 'medium');
  }, [isOpen, complaint]);

  const submit = async () => {
    const trimmedTitle = title.trim();
    if (!trimmedTitle) return;
    const fields = {
      title: trimmedTitle,
      description: description.trim() || undefined,
      customer_name: customerName.trim() || undefined,
      customer_contact: customerContact.trim() || undefined,
      category,
      severity,
    };
    if (isEdit && complaint) {
      await updateMutation.mutateAsync({ id: complaint.id, ...fields });
    } else {
      await createMutation.mutateAsync(fields);
    }
    onClose();
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} size="lg">
      <ModalHeader onClose={onClose}>
        {isEdit ? t('complaints.modal.editTitle') : t('complaints.modal.createTitle')}
      </ModalHeader>
      <ModalBody className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-foreground mb-1">
            {t('complaints.modal.titleLabel')}
          </label>
          <Input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t('complaints.modal.titlePlaceholder')}
            maxLength={200}
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-foreground mb-1">
            {t('complaints.modal.descriptionLabel')}
          </label>
          <Textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t('complaints.modal.descriptionPlaceholder')}
            rows={4}
          />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className="block text-sm font-medium text-foreground mb-1">
              {t('complaints.modal.customerNameLabel')}
            </label>
            <Input
              value={customerName}
              onChange={(e) => setCustomerName(e.target.value)}
              maxLength={200}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-foreground mb-1">
              {t('complaints.modal.customerContactLabel')}
            </label>
            <Input
              value={customerContact}
              onChange={(e) => setCustomerContact(e.target.value)}
              maxLength={200}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-foreground mb-1">
              {t('complaints.modal.categoryLabel')}
            </label>
            <Select
              value={category}
              onChange={(e) => setCategory(e.target.value as ComplaintCategory)}
            >
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {t(`complaints.category.${c}`)}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <label className="block text-sm font-medium text-foreground mb-1">
              {t('complaints.modal.severityLabel')}
            </label>
            <Select
              value={severity}
              onChange={(e) => setSeverity(e.target.value as ComplaintSeverity)}
            >
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {t(`complaints.severity.${s}`)}
                </option>
              ))}
            </Select>
          </div>
        </div>
      </ModalBody>
      <ModalFooter>
        <Button variant="ghost" onClick={onClose}>
          {t('complaints.modal.cancel')}
        </Button>
        <Button onClick={() => void submit()} disabled={!title.trim()} loading={pending}>
          {isEdit ? t('complaints.modal.save') : t('complaints.modal.create')}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
