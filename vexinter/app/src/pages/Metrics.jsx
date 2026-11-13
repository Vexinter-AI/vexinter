import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { api } from '../api';
import { Page, KPI, Spinner, Donut, Bars, STATE_COLOR } from '../components/ui';

export default function Metrics() {
  const { t } = useTranslation();
  const [m, setM] = useState(null);
  const nav = useNavigate();
  useEffect(() => { api('/v1/metrics').then(setM)
    .catch(e => e.auth && nav('/login')); }, []);
  if (!m) return <Spinner />;
  const donut = Object.entries(m.final_states || {})
    .map(([k, v]) => ({ name: t(`state.${k}`), value: v,
      color: STATE_COLOR[k] || '#64748b' }));
  const rej = { ...(m.message_rejections_by_reason || {}),
    ...(m.tool_rejections_by_reason || {}) };
  const bars = Object.entries(rej).map(([name, value]) => ({ name, value }));
  return <Page>
    <h1>{t('metrics.title')}</h1>
    <p className="muted">{t('metrics.subtitle')}</p>
    <div className="cards">
      <KPI value={m.executions} label={t('kpi.executions')} accent="acc-grad" />
      <KPI value={m.false_successes_detected} label={t('kpi.falseSuccess')} accent="acc-red" />
      <KPI value={m.discrepancies} label={t('kpi.discrepancies')} />
      <KPI value={m.retries_or_compensations} label={t('kpi.retriesComp')} />
      <KPI value={m.human_decisions_requested} label={t('kpi.humanDecisions')} />
      <KPI value={m.authentication_errors || 0} label={t('kpi.authErrors')} />
      <KPI value={m.integrity_failures || 0} label={t('kpi.integrityFails')} accent="acc-red" />
    </div>
    <div className="grid2">
      <div className="glass panel"><b>{t('metrics.finalStates')}</b>
        {donut.length ? <Donut data={donut} />
          : <p className="muted">{t('common.nodata')}</p>}</div>
      <div className="glass panel"><b>{t('metrics.rejectsByReason')}</b>
        {bars.length ? <Bars data={bars} />
          : <p className="muted">{t('common.nodata')}</p>}</div>
    </div>
  </Page>;
}