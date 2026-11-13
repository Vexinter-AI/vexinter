import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { api } from '../api';
import { Page, KPI, Badge, Spinner, Donut, STATE_COLOR } from '../components/ui';

export default function Dashboard() {
  const { t } = useTranslation();
  const [d, setD] = useState(null);
  const nav = useNavigate();
  const load = () => Promise.all([api('/v1/admin/summary'), api('/v1/tasks')])
    .then(([s, ts]) => setD({ s, ts }))
    .catch(e => e.auth && nav('/login'));
  useEffect(() => { load(); const i = setInterval(load, 3000);
    return () => clearInterval(i); }, []);
  if (!d) return <Spinner />;
  const m = d.s.metrics;
  const donut = Object.entries(m.final_states || {})
    .map(([k, v]) => ({ name: t(`state.${k}`), value: v,
      color: STATE_COLOR[k] || '#64748b' }));
  return <Page>
    <h1>{t('nav.panel')}</h1>
    <p className="muted">Evidencia, no afirmaciones — {t('exec.subtitle')}</p>
    <div className="cards">
      <KPI value={m.executions} label={t('kpi.executions')} accent="acc-grad" />
      <KPI value={m.false_successes_detected} label={t('kpi.falseSuccess')} accent="acc-red" />
      <KPI value={m.discrepancies} label={t('kpi.discrepancies')} />
      <KPI value={m.retries_or_compensations} label={t('kpi.retriesComp')} />
      <KPI value={m.human_decisions_requested} label={t('kpi.humanDecisions')} />
      <KPI value={d.s.cases} label={t('kpi.pendingCases')} />
    </div>
    <div className="grid2">
      <div className="glass panel"><b>{t('metrics.finalStates')}</b>
        {donut.length ? <Donut data={donut} /> : <p className="muted">{t('common.nodata')}</p>}
      </div>
      <div className="glass panel">
        <div className="panel-head"><b>{t('nav.executions')}</b>
          <Link className="chip" to="/executions">{t('exec.all')}</Link></div>
        <table><tbody>
          {d.ts.slice(0, 8).map(x => <tr className="click" key={x.task_id}
            onClick={() => nav(`/tasks/${x.task_id}`)}>
            <td className="mono" style={{ color: 'var(--p1)' }}>
              {x.task_id.slice(0, 14)}…</td>
            <td>{(x.objective || '').slice(0, 44)}</td>
            <td><Badge state={x.state} /></td></tr>)}
        </tbody></table>
      </div>
    </div>
  </Page>;
}