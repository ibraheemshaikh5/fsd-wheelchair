// Small DOM helpers shared by the pages. Thin browser wrappers; exercised by
// the Playwright test rather than unit tests.

import type { StatusRow } from './status';

/** The element with `id`, or a clear error naming the page whose markup is missing it. */
export function byId<T extends HTMLElement>(page: string, id: string): T {
  const element = document.getElementById(id);
  if (element === null) throw new Error(`${page} page: missing element #${id}`);
  return element as T;
}

/** Replaces a <dl> with one label/value pair per row, coloured by level. */
export function renderRows(list: HTMLDListElement, rows: readonly StatusRow[]): void {
  list.replaceChildren(
    ...rows.flatMap((row) => {
      const term = document.createElement('dt');
      term.textContent = row.label;
      const value = document.createElement('dd');
      value.textContent = row.value;
      value.className = `level-${row.level}`;
      return [term, value];
    }),
  );
}
