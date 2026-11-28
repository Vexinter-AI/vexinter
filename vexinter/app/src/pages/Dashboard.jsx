import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { api } from '../api';
import { Page, KPI, Badge, Spinner, Donut, STATE_COLOR } from '../components/ui';

export default function Dashboard() {
  const { t } = useTranslation();
  const [d, setD] = useState(null);
  const nav = useNavigate();
  const load = () => Promise.all([api('/v1/admin/summary'), api('/v1/tasks'),
      api('/v1/billing/usage').catch(() => null)])
    .then(([s, ts, u]) => setD({ s, ts, u }))
    .catch(e => e.auth && nav('/login'));
  useEffect(() => { load(); const i = setInterval(load, 3000);
    return () => clearInterval(i); }, []);
  if (!d) return <Spinner />;
  const m = d.s.metrics;
  const u = d.u;
  const used = u ? u.tasks_used : 0;
  const limit = u ? u.task_limit : 0;
  const pct = limit > 0 ? Math.min(100, Math.round(used / limit * 100)) : 0;
  const barColor = pct >= 90 ? '#ff5d5d' : pct >= 70 ? '#f0b429' : 'var(--accent)';
  const donut = Object.entries(m.final_states || {})
    .map(([k, v]) => ({ name: t(`state.${k}`), value: v,
      color: STATE_COLOR[k] || '#64748b' }));
  return <Page>
    <h1>{t('nav.panel')}</h1>
    {u && <div className="glass quota-card">
      <div className="quota-head">
        <div>
          <b>{t('billing.title')}</b>
          <span className="muted">
            {' — '}{t('billing.used', { used, limit })}
            {u.period_end && <> · {t('billing.resets',
              { date: new Date(u.period_end).toLocaleDateString('en-US',
                { timeZone: 'UTC', month: 'short', day: 'numeric' }) + ' UTC' })}</>}
          </span>
        </div>
        {u.billing_portal_url && u.tier !== 'community' &&
          <a className="chip" href={u.billing_portal_url}
            target="_blank" rel="noreferrer">
            {t('billing.manage')}</a>}
      </div>
      <div className="quota-track">
        <div className="quota-fill" style={{ width: pct + '%',
          background: barColor }} />
      </div>
      {u.hard_block && pct >= 100 &&
        <p className="quota-warn">{t('billing.blocked')}</p>}
    </div>}
    <div className="cards">
      <KPI value={m.executions} label={t('kpi.executions')} accent="acc-grad" />
      <KPI value={m.false_successes_detected} label={t('kpi.falseSuccess')} accent="acc-red" />
      <KPI value={m.discrepancies} label={t('kpi.discrepancies')} />
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