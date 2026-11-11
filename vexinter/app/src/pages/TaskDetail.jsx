import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { api } from '../api';
import { useTheme } from '../theme';
import Graph from '../components/Graph';
import { Page, Badge, Spinner } from '../components/ui';

export default function TaskDetail() {
  const { id } = useParams();
  const { t } = useTranslation();
  const { theme } = useTheme();
  const nav = useNavigate();
  const [d, setD] = useState(null);
  const [sel, setSel] = useState(null);
  useEffect(() => {
    api(`/v1/admin/tasks/${id}`).then(setD)
      .catch(e => e.auth && nav('/login'));
  }, [id]);
  if (!d) return <Spinner />;
  return <Page>
    <h1 className="mono" style={{ fontSize: 16 }}>
      <span className="grad-text">exec</span> {d.task_id.slice(0, 20)}…</h1>
    <p className="muted">{d.objective} · agent <b>{d.agent || '—'}</b></p>
    <p><Badge state={d.state} big /></p>
    {d.insufficient && <div className="warnbox">⚠ {t('detail.insufficient')}</div>}
    {d.audit.ok
      ? <div className="okbox">✓ {t('detail.auditOk', { n: d.audit.events_checked })}</div>
      : <div className="badbox">✕ {t('detail.auditBad')}: {d.audit.problems.join(' · ')}</div>}
    <div className="glass panel"><b>{t('detail.verdicts')}</b>
      {d.verdicts.length ? <table><thead><tr>
        <th>{t('detail.moment')}</th><th>{t('detail.type')}</th>
        <th>{t('detail.scope')}</th></tr></thead><tbody>
        {d.verdicts.map((v, i) => <tr key={i}>
          <td className="muted">{new Date(v.ts).toLocaleTimeString()}</td>
          <td><Badge state={
            v.status === 'met' ? 'satisfied'
            : v.status === 'not_met' ? 'failed' : 'inconclusive'} />
            {v.provisional && ' ◔'}</td>
          <td className="muted">{v.scope}</td></tr>)}
      </tbody></table> : <p className="muted">{t('detail.noVerdicts')}</p>}
    </div>
    <div className="glass panel"><b>{t('detail.graph')}</b>
      <div className="split" style={{ marginTop: 10 }}>
        <Graph graph={d.graph} theme={theme} onSelect={setSel} />
        <div className="glass node-info">
          <h3 style={{ marginTop: 0 }}>{t('detail.nodeTitle')}</h3>
          {sel ? <>
            <Badge state={sel.type.startsWith('verifier.') ? 'satisfied' : 'in_progress'} />
            {' '}<span className="mono muted">{sel.id.slice(0, 16)}…</span>
            <pre>{JSON.stringify({ type: sel.type, ts: sel.ts,
              label: sel.label, relations: sel.edges }, null, 2)}</pre>
          </> : <p className="muted">{t('detail.nodeHint')}</p>}
        </div>
      </div>
    </div>
    <div className="glass panel"><b>{t('detail.timeline')}</b>
      <table><thead><tr><th>#</th><th>{t('detail.moment')}</th>
        <th>{t('detail.type')}</th><th>{t('detail.detail')}</th>
        <th>{t('detail.relations')}</th><th>{t('detail.payload')}</th></tr></thead>
      <tbody>{d.events.map(e => <tr key={e.seq}>
        <td className="muted">{e.seq}</td>
        <td className="muted">{new Date(e.ts).toLocaleTimeString()}</td>
        <td><span className="badge st-in_progress" style={{ fontSize: 10 }}>
          {e.type}</span></td>
        <td>{e.label || '—'}</td>
        <td className="muted" style={{ fontSize: 11 }}>{e.rels}</td>
        <td>{e.payload !== '{}' ? <details>
          <summary>{t('detail.see')}</summary>
          <pre style={{ maxWidth: 340 }}>{e.payload}</pre></details>
          : <span className="muted">—</span>}</td>
      </tr>)}</tbody></table>
    </div>
  </Page>;
}