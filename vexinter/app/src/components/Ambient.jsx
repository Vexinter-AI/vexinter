import { useEffect } from "react";

// Ambient backdrop ported from the landing page: dot-grid, scanning
// highlight, horizon glow and a cursor-tracking glow/spotlight on
// .glass surfaces. Pointer position is published as --mx/--my on
// <html>; each hovered .glass gets --sx/--sy for its radial spotlight.
export default function Ambient() {
  useEffect(() => {
    const root = document.documentElement;
    const fine = window.matchMedia("(pointer: fine)").matches;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!fine || reduce) return;

    let lit = null;
    const onMove = (e) => {
      root.style.setProperty("--mx", `${e.clientX}px`);
      root.style.setProperty("--my", `${e.clientY}px`);
      const card = e.target instanceof Element ? e.target.closest(".glass") : null;
      if (card !== lit) {
        if (lit) lit.removeAttribute("data-lit");
        lit = card;
        if (lit) lit.setAttribute("data-lit", "true");
      }
      if (card) {
        const r = card.getBoundingClientRect();
        card.style.setProperty("--sx", `${e.clientX - r.left}px`);
        card.style.setProperty("--sy", `${e.clientY - r.top}px`);
      }
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => window.removeEventListener("pointermove", onMove);
  }, []);

  return (
    <div className="ambient" aria-hidden="true">
      <div className="horizon" />
      <div className="dot-grid" />
      <div className="dot-grid-scan" />
      <div className="dot-grid-lit" />
      <div className="cursor-glow" />
    </div>
  );
}
