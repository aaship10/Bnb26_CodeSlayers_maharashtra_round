import { useEffect } from 'react';

/** Sets the tab title (and what screen readers announce on navigation). Restores nothing: every page sets its own. */
export function useDocumentTitle(title: string | undefined): void {
  useEffect(() => {
    if (title) document.title = `${title} · Fair Drop`;
  }, [title]);
}
