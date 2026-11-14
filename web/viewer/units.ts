// Unit formatting shared by the viewer's status rows.

const MS_PER_S = 1000;
/** Metres per second to miles per hour: 3600 s per hour over 1609.344 m per mile. */
export const MPS_TO_MPH = 3600 / 1609.344;

export function formatAge(ms: number | null): string {
  if (ms === null) return '—';
  return ms < MS_PER_S ? `${Math.round(ms)} ms` : `${(ms / MS_PER_S).toFixed(1)} s`;
}
