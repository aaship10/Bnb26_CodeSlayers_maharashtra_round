import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';

interface AnnouncerApi {
  /** Speak a short status change to screen readers (polite). */
  announce: (message: string) => void;
}

const Ctx = createContext<AnnouncerApi>({ announce: () => undefined });

/**
 * One shared polite live region for state changes ("Drawing has started",
 * "You won a seat"). Components call announce(); they don't each own a region.
 */
export function AnnouncerProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState('');
  const flip = useRef(false);

  const announce = useCallback((next: string) => {
    // Re-announcing the same sentence needs the DOM text to change, so alternate a trailing space.
    flip.current = !flip.current;
    setMessage(flip.current ? next : `${next} `);
  }, []);

  const api = useMemo(() => ({ announce }), [announce]);

  return (
    <Ctx.Provider value={api}>
      {children}
      <div aria-live="polite" aria-atomic="true" className="sr-only" data-testid="announcer">
        {message}
      </div>
    </Ctx.Provider>
  );
}

export function useAnnounce(): AnnouncerApi['announce'] {
  return useContext(Ctx).announce;
}
