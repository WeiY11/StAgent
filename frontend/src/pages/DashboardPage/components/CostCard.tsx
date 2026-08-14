import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Wallet } from 'lucide-react';
import { Card, CardContent } from '@/components/ui';
import { useCosts, useCostsByModel } from '@/hooks/useCosts';

function formatUsd(value: number): string {
  return `$${value.toFixed(2)}`;
}

function formatLatency(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function CostCard() {
  const { t } = useTranslation();
  const { data: costs, isLoading } = useCosts(30);
  const { data: byModel } = useCostsByModel(30);

  const budget = costs?.budget;
  const today = costs?.days[costs.days.length - 1];

  const maxDailyCost = useMemo(() => {
    if (!costs?.days.length) return 1;
    return Math.max(...costs.days.map((d) => d.cost_usd), 0.01) * 1.2;
  }, [costs]);

  const budgetPct = budget?.monthly_budget_usd ? Math.min(budget.pct, 100) : 0;
  const exceeded = Boolean(budget?.exceeded);
  const nearLimit = Boolean(
    budget?.monthly_budget_usd && !exceeded && budget.pct >= 80,
  );

  const barColor = exceeded
    ? 'bg-red-500'
    : nearLimit
      ? 'bg-yellow-500'
      : 'bg-blue-500';

  return (
    <Card>
      <div className="p-4 border-b border-gray-200 dark:border-gray-700">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            {t('dashboard.costCard.title')}
          </h2>
          <Wallet className="w-4 h-4 text-gray-400" />
        </div>
        {exceeded && (
          <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-red-600 dark:text-red-400">
            <AlertTriangle className="w-3.5 h-3.5" />
            {t('dashboard.costCard.budgetExceeded')}
          </p>
        )}
        {nearLimit && (
          <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-yellow-600 dark:text-yellow-400">
            <AlertTriangle className="w-3.5 h-3.5" />
            {t('dashboard.costCard.budgetNearLimit')}
          </p>
        )}
      </div>
      <CardContent className="space-y-4">
        {/* Monthly budget progress (soft reminder) */}
        <div>
          <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
            <span>{t('dashboard.costCard.monthlyBudget')}</span>
            <span className={exceeded ? 'text-red-600 dark:text-red-400 font-medium' : ''}>
              {budget?.monthly_budget_usd != null
                ? `${formatUsd(budget.month_to_date_usd)} / ${formatUsd(budget.monthly_budget_usd)}`
                : `${formatUsd(budget?.month_to_date_usd ?? 0)} · ${t('dashboard.costCard.budgetUnset')}`}
            </span>
          </div>
          <div className="h-2 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-300 ${barColor}`}
              style={{ width: budgetPct > 0 ? `${budgetPct}%` : '0%' }}
              role="progressbar"
              aria-label={t('dashboard.costCard.monthlyBudget')}
              aria-valuenow={Math.round(budget?.pct ?? 0)}
              aria-valuemin={0}
              aria-valuemax={100}
            />
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {t('dashboard.costCard.budgetHint')}
          </p>
        </div>

        {/* Quick stats */}
        <div className="grid grid-cols-3 gap-3 text-center">
          <div>
            <div className="text-lg font-semibold text-gray-900 dark:text-white">
              {formatUsd(budget?.month_to_date_usd ?? 0)}
            </div>
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {t('dashboard.costCard.monthSpend')}
            </div>
          </div>
          <div>
            <div className="text-lg font-semibold text-gray-900 dark:text-white">
              {formatUsd(today?.cost_usd ?? 0)}
            </div>
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {t('dashboard.costCard.todaySpend')}
            </div>
          </div>
          <div>
            <div className="text-lg font-semibold text-gray-900 dark:text-white">
              {costs?.totals.runs ?? 0}
            </div>
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {t('dashboard.costCard.runs')}
            </div>
          </div>
        </div>

        {/* Daily cost bars (last 30 days) */}
        <div>
          <div className="text-xs text-gray-500 dark:text-gray-400 mb-2">
            {t('dashboard.costCard.dailyCost')}
          </div>
          {isLoading ? (
            <div className="h-32 flex items-center justify-center text-gray-500 dark:text-gray-400">
              {t('common.loading')}
            </div>
          ) : costs && costs.days.some((d) => d.cost_usd > 0) ? (
            <div className="h-32 flex items-end gap-0.5" data-testid="cost-bars">
              {costs.days.map((day) => (
                <div
                  key={day.date}
                  className={`flex-1 rounded-t ${exceeded ? 'bg-red-400' : 'bg-blue-400'}`}
                  style={{ height: `${Math.max((day.cost_usd / maxDailyCost) * 100, day.cost_usd > 0 ? 4 : 0)}%` }}
                  title={`${day.date}: ${formatUsd(day.cost_usd)}`}
                />
              ))}
            </div>
          ) : (
            <div className="h-32 flex items-center justify-center text-gray-500 dark:text-gray-400">
              {t('common.noData')}
            </div>
          )}
        </div>

        {/* Per-model breakdown */}
        {byModel && byModel.length > 0 && (
          <div>
            <div className="text-xs text-gray-500 dark:text-gray-400 mb-2">
              {t('dashboard.costCard.byModel')}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                    <th className="py-1.5 pr-2 font-medium">{t('dashboard.costCard.model')}</th>
                    <th className="py-1.5 pr-2 font-medium text-right">{t('dashboard.costCard.modelRuns')}</th>
                    <th className="py-1.5 pr-2 font-medium text-right">{t('dashboard.costCard.modelCost')}</th>
                    <th className="py-1.5 pr-2 font-medium text-right">{t('dashboard.costCard.modelLatency')}</th>
                    <th className="py-1.5 pr-2 font-medium text-right">{t('dashboard.costCard.modelTokens')}</th>
                    <th className="py-1.5 font-medium text-right">{t('dashboard.costCard.modelSuccessRate')}</th>
                  </tr>
                </thead>
                <tbody>
                  {byModel.slice(0, 5).map((row) => (
                    <tr key={row.model} className="border-b border-gray-100 dark:border-gray-800">
                      <td className="py-1.5 pr-2 truncate max-w-32" title={row.model}>{row.model}</td>
                      <td className="py-1.5 pr-2 text-right">{row.runs}</td>
                      <td className="py-1.5 pr-2 text-right">{formatUsd(row.cost_usd)}</td>
                      <td className="py-1.5 pr-2 text-right">{formatLatency(row.avg_latency_ms)}</td>
                      <td className="py-1.5 pr-2 text-right">{row.total_tokens.toLocaleString()}</td>
                      <td className="py-1.5 text-right">{row.success_rate}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
