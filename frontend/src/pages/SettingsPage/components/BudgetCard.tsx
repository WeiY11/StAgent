import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, CardContent, CardHeader, CardTitle, Input } from '@/components/ui';
import { costsApi } from '@/api/stats';
import { useToast } from '@/components/ui/Toaster';

export function BudgetCard() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    costsApi
      .getBudget()
      .then((status) => {
        if (!cancelled) {
          setValue(status.monthly_budget_usd != null ? String(status.monthly_budget_usd) : '');
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const save = async () => {
    const parsed = value.trim() === '' ? null : Number(value);
    if (parsed != null && (!Number.isFinite(parsed) || parsed < 0)) {
      toast({
        variant: 'error',
        title: t('settings.budget.saveFailed'),
        description: t('settings.budget.description'),
      });
      return;
    }
    setBusy(true);
    try {
      const status = await costsApi.putBudget(parsed ?? 0);
      if (status.monthly_budget_usd == null) {
        setValue('');
        toast({ variant: 'success', title: t('settings.budget.cleared') });
      } else {
        toast({ variant: 'success', title: t('settings.budget.saved') });
      }
    } catch (error) {
      toast({
        variant: 'error',
        title: t('settings.budget.saveFailed'),
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('settings.budget.title')}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">{t('settings.budget.description')}</p>
        <div className="flex gap-2">
          <Input
            type="number"
            min={0}
            step="0.5"
            value={value}
            disabled={busy}
            onChange={(event) => setValue(event.target.value)}
            placeholder={t('settings.budget.placeholder')}
            aria-label={t('settings.budget.label')}
          />
          <Button disabled={busy} onClick={() => void save()}>
            {busy ? t('settings.budget.saving') : t('settings.budget.save')}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
