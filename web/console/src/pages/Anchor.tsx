import { type ReactNode, useEffect } from 'react';

/** A panel a row of the list leads to: the page scrolls to it when its name is in the address. */
export function Anchor({ id, children }: { id: string; children: ReactNode }) {
  return (
    <div id={id} className="min-w-0 scroll-mt-20 empty:hidden">
      {children}
    </div>
  );
}

/**
 * Brings the panel named in the address into view. The panels above it load after the
 * page does and push it down, so it is brought back for a few seconds, and left alone as
 * soon as the page is scrolled by hand.
 */
export function useAnchor(view: string) {
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (!id) return;
    let tries = 0;
    let timer = 0;
    let stopped = false;
    const stop = () => {
      stopped = true;
    };
    const events = ['wheel', 'touchmove', 'keydown', 'mousedown'] as const;
    events.forEach((name) => window.addEventListener(name, stop, { passive: true }));
    const go = () => {
      if (stopped) return;
      document.getElementById(id)?.scrollIntoView({ block: 'start' });
      tries += 1;
      if (tries < 10) timer = window.setTimeout(go, 400);
    };
    go();
    return () => {
      window.clearTimeout(timer);
      events.forEach((name) => window.removeEventListener(name, stop));
    };
  }, [view]);
}
