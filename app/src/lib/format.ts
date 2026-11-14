/** "520 ft" close in, "0.4 mi" further out. */
export function distanceLabel(metres: number): string {
  const miles = metres / 1609.344
  if (miles >= 0.1) return `${miles.toFixed(1)} mi`
  const feet = metres * 3.28084
  return `${feet < 100 ? Math.round(feet / 5) * 5 : Math.round(feet / 10) * 10} ft`
}

export const mpsToMph = (mps: number) => mps * 2.23694

/** "3:56 pm" */
export function clockTime(date: Date): string {
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }).toLowerCase()
}
