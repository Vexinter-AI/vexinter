import { useEffect, useRef } from 'react';
import cytoscape from 'cytoscape';
import dagre from 'cytoscape-dagre';
cytoscape.use(dagre);

const NODE_C = { life: '#5b6b7c', tool: '#0f9d8a', verify: '#22b45a',
  recovery: '#c47a17', msg: '#8b5cf6', ident: '#64748b', mem: '#a855f7' };
const cls = t => t.startsWith('tool.') ? 'tool'
  : t.startsWith('verifier.') ? 'verify'
  : (t.startsWith('recovery.') || t.startsWith('correction.')) ? 'recovery'
  : t.startsWith('message.') ? 'msg'
  : t.startsWith('identity.') ? 'ident'
  : (t.startsWith('memory.') || t.startsWith('followup.')) ? 'mem' : 'life';

export default function Graph({ graph, theme, onSelect }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!graph || !graph.nodes || !graph.nodes.length || !ref.current) return;
    const isDark = theme !== 'light';
    const fg = isDark ? '#eafbef' : '#0c1f12';
    const els = [
      ...graph.nodes.map(n => ({ data: {
        id: n.id, label: (n.label || n.type).slice(0, 32),
        type: n.type, ts: n.ts }, classes: cls(n.type) })),
      ...graph.edges.map(e => ({ data: {
        source: e.source, target: e.target, rel: e.rel }, classes: e.rel })),
    ];
    const cy = cytoscape({
      container: ref.current, elements: els, wheelSensitivity: .25,
      style: [
        { selector: 'node', style: {
          shape: 'round-rectangle',
          'background-color': ele => NODE_C[cls(ele.data('type'))] || NODE_C.life,
          'border-width': 1,
          'border-color': isDark ? 'rgba(255,255,255,.28)' : 'rgba(0,0,0,.3)',
          label: 'data(label)', color: fg, 'font-size': 9,
          'font-family': 'Inter, sans-serif', 'text-wrap': 'wrap',
          width: 'label', height: 'label', padding: '9' } },
        { selector: 'edge', style: { width: 1.6, 'curve-style': 'bezier',
          'line-color': '#64748b', 'target-arrow-shape': 'triangle',
          'arrow-scale': .7, 'target-arrow-color': '#64748b' } },
        { selector: 'edge.compenso_a', style: { 'line-color': '#ff5d5d',
          'target-arrow-color': '#ff5d5d', 'line-style': 'dashed' } },
        { selector: 'edge.depende_de', style: { 'line-color': '#36EB50',
          'target-arrow-color': '#36EB50', 'line-style': 'dashed' } },
        { selector: 'edge.fue_verificado_por', style: {
          'line-color': '#22b45a', 'target-arrow-color': '#22b45a' } },
      ],
      layout: { name: 'dagre', rankDir: 'LR', nodeSep: 36, rankSep: 95,
        padding: 18 },
    });
    cy.on('tap', 'node', ev => {
      const d = ev.target.data();
      const edges = ev.target.connectedEdges().map(ed => ({
        rel: ed.data('rel'),
        dir: ed.target().id() === d.id ? 'in' : 'out',
        other: ed.source().id() === d.id ? ed.target().id() : ed.source().id(),
      }));
      onSelect && onSelect(d, edges);
    });
    return () => cy.destroy();
  }, [graph, theme, onSelect]);
  if (!graph || !graph.nodes || !graph.nodes.length)
    return <div className="graph-canvas" style={{ display: 'grid',
      placeItems: 'center', color: 'var(--muted)' }}>—</div>;
  return <div ref={ref} className="graph-canvas" />;
}