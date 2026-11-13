import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { api } from '../api';
import { Page, Spinner, Modal } from '../components/ui';

const csv = v => v.split(',').map(s => s.trim()).filter(Boolean);

export default function Agents() {
  const { t } = useTranslation();
  const [d, setD] = useState(null);
  const [modal, setModal] = useState(null);
  const [form, setForm] = useState({ agent_id: '', display_name: '',
    allowed_tools: '', allowed_recipients: '' });
  const nav = useNavigate();
  const load = () => Promise.all([
    api('/v1/identity/agents'), api('/v1/identity/credentials'),
    api('/v1/identity/instances')])
    .then(([a, c, i]) => setD({ a, c, i }))
    .catch(e => e.auth && nav('/login'));
  useEffect(() => { load(); }, []);
  if (!d) return <Spinner />;
  async function enroll(e) {
    e.preventDefault();
    await api('/v1/identity/agents', { method: 'POST', body: {
      agent_id: form.agent_id, display_name: form.display_name,
      allowed_tools: csv(form.allowed_tools).length
        ? csv(form.allowed_tools) : null,
      allowed_recipients: csv(form.allowed_recipients).length
        ? csv(form.allowed_recipients) : null } });
    setForm({ agent_id: '', display_name: '', allowed_tools: '',
      allowed_recipients: '' });
    load();
  }
  return <Page>
    <h1>{t('agents.title')}</h1>
    <p className="muted">{t('agents.note')}</p>
    <div className="grid2">
      <form className="glass panel" onSubmit={enroll}>
        <b>{t('agents.enroll')}</b>
        <label>{t('agents.id')}</label>
        <input value={form.agent_id} required
          onChange={e => setForm({ ...form, agent_id: e.target.value })} />
        <label>{t('agents.name')}</label>
        <input value={form.display_name}
          onChange={e => setForm({ ...form, display_name: e.target.value })} />
        <label>{t('agents.tools')} (CSV)</label>
        <input placeholder="crm.create_customer, billing.create_invoice"
          value={form.allowed_tools}
          onChange={e => setForm({ ...form, allowed_tools: e.target.value })} />
        <label>{t('agents.recipients')} (CSV)</label>
        <input value={form.allowed_recipients}
          onChange={e => setForm({ ...form, allowed_recipients: e.target.value })} />
        <button className="btn btn-primary" style={{ marginTop: 16 }}>
          {t('agents.enroll')}</button>
      </form>
      <div className="glass panel"><b>{t('agents.title')}</b>
        <table><tbody>{d.a.map(a => <tr key={a.agent_id}>
          <td><b>{a.agent_id}</b></td>
          <td className="muted" style={{ fontSize: 11 }}>
            {a.allowed_tools === null ? t('agents.allDev')
              : (a.allowed_tools || []).join(', ')}</td>
        </tr>)}</tbody></table>
      </div>
    </div>
    <div className="glass panel"><b>{t('agents.creds')}</b>
      <table><thead><tr><th>ID</th><th>{t('agents.agent')}</th>
        <th>{t('agents.expiry')}</th><th/></tr></thead><tbody>
        {d.c.map(c => <tr key={c.credential_id}>
          <td className="mono muted">{c.credential_id.slice(0, 16)}…</td>
          <td>{c.agent_id}</td>
          <td className="muted">{new Date(c.expires_at).toLocaleTimeString()}</td>
          <td>{c.status === 'active'
            ? <button className="btn btn-danger"
                onClick={() => setModal({ kind: 'cred', id: c.credential_id })}>
                {t('agents.revoke')}</button>
            : <span className="badge st-failed">
                {t('agents.revoked')}</span>}</td>
        </tr>)}</tbody></table>
    </div>
    <div className="glass panel"><b>{t('agents.insts')}</b>
      <table><thead><tr><th>ID</th><th>{t('agents.agent')}</th><th/></tr></thead>
      <tbody>{d.i.map(i => <tr key={i.instance_id}>
        <td className="mono muted">{i.instance_id.slice(0, 16)}…</td>
        <td>{i.agent_id}</td>
        <td>{i.status === 'active'
          ? <button className="btn btn-danger"
              onClick={() => setModal({ kind: 'inst', id: i.instance_id })}>
              {t('agents.revoke')}</button>
          : <span className="badge st-failed">{t('agents.revoked')}</span>}</td>
      </tr>)}</tbody></table>
    </div>
    <Modal open={!!modal} onClose={() => setModal(null)}
      title={t('cases.confirmTitle')}>
      {modal && <>
        <p>{t('cases.confirmBody', {
          action: t('agents.revoke'), id: modal.id.slice(0, 14),
          risk: 'alto' })}</p>
        <div style={{ display: 'flex', gap: 10, marginTop: 18 }}>
          <button className="btn btn-ghost" style={{ flex: 1 }}
            onClick={() => setModal(null)}>{t('cases.cancel')}</button>
          <button className="btn btn-danger" style={{ flex: 1 }}
            onClick={async () => {
              const m = modal; setModal(null);
              if (m.kind === 'cred') await api(
                `/v1/identity/credentials/${m.id}`,
                { method: 'DELETE', query: { reason: 'revocada desde la consola' } });
              else await api(`/v1/identity/instances/${m.id}/revoke`,
                { method: 'POST', body: { reason: 'revocada desde la consola' } });
              load();
            }}>{t('cases.confirm')}</button>
        </div>
      </>}
    </Modal>
  </Page>;
}