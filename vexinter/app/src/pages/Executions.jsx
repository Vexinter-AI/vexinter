import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { api } from '../api';
import { Page, Badge, Spinner, STATES, MACHINE } from '../components/ui';

export default function Executions() {
  const { t } = useTranslation();
  const [tasks, setTasks] = useState(null);
  const [filt, setFilt] = useState('');
  const nav = useNavigate();
  const load = () => api('/v1/tasks').then(setTasks)
    .catch(e => e.auth && nav('/login'));
  useEffect(() => { load(); }, []);
  if (!tasks) return <Spinner />;
  const rows = tasks.filter(x => !filt || x.state === filt);
  return <Page>
    <h1>{t('nav.executions')}</h1>
    <p className="muted">{t('exec.subtitle')}</p>
    <div className="chips">
      <button className={'chip' + (filt === '' ? ' on' : '')}
        onClick={() => setFilt('')}>{t('exec.all')}</button>
      {STATES.map(s => <button key={s}
        className={'chip' + (filt === s ? ' on' : '')}
        onClick={() => setFilt(s)}>{t(`state.${s}`)}</button>)}
    </div>
    <div className="glass panel">
      <table><thead><tr>
        <th>{t('exec.task')}</th><th>{t('exec.agent')}</th>
        <th>{t('exec.objective')}</th><th>{t('exec.created')}</th><th/></tr>
      </thead><tbody>
        {rows.map(x => <tr className="click" key={x.task_id}
          onClick={() => nav(`/tasks/${x.task_id}`)}>
          <td className="mono" style={{ color: 'var(--p1)' }}>
            {x.task_id.slice(0, 16)}…</td>
          <td>{x.agent || '—'}</td>
          <td>{(x.objective || '').slice(0, 50)}</td>
          <td className="muted">{new Date(x.created_at).toLocaleString()}</td>
          <td><Badge state={x.state} /></td></tr>)}
        {!rows.length && <tr><td colSpan={5} className="muted">
          {t('common.nodata')}</td></tr>}
      </tbody></table>
    </div>
    <details className="glass panel">
      <summary style={{ fontWeight: 600 }}>{t('exec.machine')}</summary>
      <table><thead><tr><th>{t('exec.from')}</th><th>{t('exec.to')}</th></tr></thead>
      <tbody>{Object.entries(MACHINE).map(([s, to]) => <tr key={s}>
        <td><Badge state={s} /></td>
        <td className="muted">{to.map(x => t(`state.${x}`)).join(', ')}</td>
      </tr>)}</tbody></table>
    </details>
  </Page>;
}