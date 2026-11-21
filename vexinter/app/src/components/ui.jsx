import { useEffect, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import { useTranslation } from 'react-i18next';
import { PieChart, Pie, Cell, ResponsiveContainer, BarChart, Bar,
         XAxis, YAxis, Tooltip, Legend } from 'recharts';

export const STATES = ['in_progress', 'pending_verification',
  'provisionally_satisfied', 'satisfied', 'failed', 'inconclusive',
  'pending_recovery', 'pending_human_decision'];
export const STATE_COLOR = { in_progress: '#4da3ff',
  pending_verification: '#8f8fff', satisfied: '#36EB50',
  provisionally_satisfied: '#97EB11', failed: '#ff5d5d',
  inconclusive: '#ffb02e', pending_recovery: '#ff9130',
  pending_human_decision: '#ff5d8f' };
export const MACHINE = {
  in_progress: ['in_progress', 'pending_verification', 'pending_recovery',
    'pending_human_decision', 'satisfied', 'provisionally_satisfied',
    'failed', 'inconclusive'],
  pending_verification: ['satisfied', 'provisionally_satisfied',
    'failed', 'inconclusive', 'pending_recovery'],
  provisionally_satisfied: ['pending_verification', 'satisfied',
    'provisionally_satisfied', 'failed', 'inconclusive',
    'pending_recovery'],
  inconclusive: ['in_progress', 'pending_verification',
    'provisionally_satisfied', 'satisfied', 'failed', 'inconclusive',
    'pending_recovery', 'pending_human_decision'],
  pending_recovery: ['pending_recovery', 'satisfied',
    'provisionally_satisfied', 'failed', 'inconclusive',
    'pending_human_decision'],
  pending_human_decision: ['pending_human_decision', 'satisfied',
    'provisionally_satisfied', 'failed', 'inconclusive',
    'pending_recovery'],
  satisfied: ['satisfied', 'provisionally_satisfied', 'inconclusive',
    'failed', 'pending_recovery'],
  failed: ['failed', 'satisfied', 'provisionally_satisfied',
    'inconclusive', 'pending_recovery'],
};

export function Badge({ state, big }) {
  const { t } = useTranslation();
  return <span className={'badge st-' + state + (big ? ' big' : '')}>
    <span className="dot" />{t(`state.${state}`)}</span>;
}

export function Page({ children }) {
  return <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
    transition={{ duration: .3, ease: 'easeOut' }}>{children}</motion.div>;
}

export function Spinner() { return <div className="spinner" />; }

export function useCountUp(v, ms = 700) {
  const [out, setOut] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    const t0 = performance.now(), a = from.current; let raf;
    const step = t => {
      const k = Math.min(1, (t - t0) / ms);
      const n = Math.round(a + (v - a) * k); setOut(n); from.current = n;
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [v]);
  return out;
}

export function KPI({ value, label, accent }) {
  const n = useCountUp(value);
  return <motion.div className="glass card kpi" whileHover={{ y: -3 }}>
    <div className={'num' + (accent ? ' ' + accent : '')}>{n}</div>
    <div className="lbl">{label}</div></motion.div>;
}

const tipStyle = { background: 'var(--glass)', border: '1px solid var(--line)',
  borderRadius: 12, color: 'var(--fg)', fontSize: 12 };

export function Donut({ data }) {
  const total = data.reduce((a, d) => a + d.value, 0);
  return <div className="donut-wrap">
    <ResponsiveContainer width="100%" height={310}>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" innerRadius="64%"
          outerRadius="88%" paddingAngle={3} stroke="none"
          label={{ position: 'outside', fill: 'var(--muted)', fontSize: 10 }}>
          {data.map((d, i) => <Cell key={i} fill={d.color} />)}
        </Pie>
        <Tooltip contentStyle={tipStyle} />
        <Legend wrapperStyle={{ fontSize: 11 }} iconType="circle" iconSize={8}
          formatter={v => <span style={{ color: 'var(--muted)' }}>{v}</span>} />
      </PieChart></ResponsiveContainer>
    <div className="donut-center">{total}</div></div>;
}

export function Bars({ data }) {
  return <ResponsiveContainer width="100%" height={310}>
    <BarChart data={data} layout="vertical" margin={{ left: 8, right: 20 }}>
      <XAxis type="number" hide />
      <YAxis type="category" dataKey="name" width={150}
        tick={{ fill: 'var(--muted)', fontSize: 11 }} axisLine={false}
        tickLine={false} />
      <Tooltip cursor={{ fill: 'rgba(128,128,128,.08)' }} contentStyle={tipStyle} />
      <Bar dataKey="value" fill="#36EB50" radius={[0, 7, 7, 0]} barSize={16} />
    </BarChart></ResponsiveContainer>;
}

export function Modal({ open, onClose, title, children }) {
  if (!open) return null;
  return <div className="modal-backdrop" onClick={onClose}>
    <motion.div className="glass modal" onClick={e => e.stopPropagation()}
      initial={{ opacity: 0, scale: .94, y: 10 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      transition={{ duration: .18 }}>
      <h3 style={{ marginTop: 0 }}>{title}</h3>{children}</motion.div></div>;
}