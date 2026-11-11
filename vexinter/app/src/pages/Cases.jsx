import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { api } from '../api';
import { Page, Badge, Spinner, Modal } from '../components/ui';

export default function Cases() {
  const { t } = useTranslation();
  const [list, setList] = useState(null);
  const [prev, setPrev] = useState({});
  const [modal, setModal] = useState(null);
  const nav = useNavigate();
  const load = () => api('/v1/cases')
    .then(async ls => {
      const det = await Promise.all(
        ls.map(c => api(`/v1/cases/${c.case.id}`)));
      setList(det); return det;
    }).catch(e => e.auth && nav('/login'));
  useEffect(() => { load(); }, []);
  if (!list) return <Spinner />;
  async function preview(cid) {
    setPrev(p => ({ ...p, [cid]: '…' }));
    try {
      const result = await api(`/v1/cases/${cid}/preview`, {
        method: 'POST'
      });
  setPrev(p => ({ ...p, [cid]: result }));
    } catch {
      setPrev(p => ({ ...p, [cid]: null }));
    }
  }
  async function decide(cid, action) {
    setModal(null);
    await api(`/v1/cases/${cid}/decide`, { method: 'POST',
      body: { action, operator: t('cases.operator') } });
    load();
  }
  return <Page>
    <h1>{t('cases.title')}</h1>
    <p className="muted">{t('cases.subtitle')}</p>
    {!list.length && <div className="okbox">✓ {t('cases.noCases')}</div>}
    {list.map(c => { const k = c.case.id, p = prev[k]; return (
      <div className="glass panel" key={k}>
        <div className="panel-head">
          <b className="mono">{k.slice(0, 20)}…</b>
          <span className="muted">{c.objective}</span></div>
        <table><tbody>
          <tr><th>{t('cases.tool')}</th><td>{c.case.tool || '—'}</td>
            <th>{t('cases.verdict')}</th>
            <td><Badge state={c.case.verdict_status === 'met' ? 'satisfied'
              : c.case.verdict_status === 'not_met' ? 'failed'
              : 'inconclusive'} /></td></tr>
          <tr><th>{t('cases.attempts')}</th><td>{c.case.attempts}</td>
            <th>{t('cases.created')}</th>
            <td className="muted">
              {new Date(c.case.created_at).toLocaleTimeString()}</td></tr>
        </tbody></table>
        <b className="muted">{t('cases.evidence')}</b>
        <table><tbody>{(c.evidence || []).slice(-6).map(e => <tr key={e.id}>
          <td className="muted" style={{ width: 90 }}>
            {new Date(e.ts).toLocaleTimeString()}</td>
          <td><span className="badge st-in_progress" style={{ fontSize: 10 }}>
            {e.type}</span></td>
        </tr>)}</tbody></table>
        {p && p !== '…' && <div className={p.would_skip_execution
            ? 'okbox' : 'warnbox'} style={{ marginTop: 8 }}>
          {p.would_skip_execution ? '✓ ' + t('cases.wouldSkip')
            : `state: ${p.status_now}`}
        </div>}
        <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
          <button className="btn btn-ghost" onClick={() => preview(k)}
            disabled={p === '…'}>
            {p === '…' ? t('cases.previewing') : t('cases.preview')}</button>
          {c.options.map(o => <button key={o.action}
            className={'btn ' + (o.risk === 'alto' ? 'btn-danger' : 'btn-primary')}
            onClick={() => setModal({ cid: k, o })}>{o.action}</button>)}
        </div>
      </div>); })}
    <Modal open={!!modal} onClose={() => setModal(null)}
      title={t('cases.confirmTitle')}>
      {modal && <>
        <p style={{ fontSize: 14 }}>
          {t('cases.confirmBody', { action: modal.o.action,
            id: modal.cid.slice(0, 14), risk: modal.o.risk })}</p>
        {prev[modal.cid]?.would_skip_execution &&
          <div className="okbox">{t('cases.wouldSkip')}</div>}
        <div style={{ display: 'flex', gap: 10, marginTop: 18 }}>
          <button className="btn btn-ghost" style={{ flex: 1 }}
            onClick={() => setModal(null)}>{t('cases.cancel')}</button>
          <button className="btn btn-primary" style={{ flex: 1 }}
            onClick={() => decide(modal.cid, modal.o.action)}>
            {t('cases.confirm')}</button>
        </div>
      </>}
    </Modal>
  </Page>;
}