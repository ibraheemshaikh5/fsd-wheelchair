const DRAWN_PX_PER_METRE = 140
// The view's camera looks down on the chair, which shortens it.
const LOOKING_DOWN_SQUASH = 0.9

interface Props {
  /** Where the chair's ground contact point sits in the view. */
  x: number
  y: number
  pxPerMetre: number
}

/** The chair seen from behind and slightly above. Meant to sit inside an <svg>. */
export function ChairModel({ x, y, pxPerMetre }: Props) {
  const scale = pxPerMetre / DRAWN_PX_PER_METRE
  return (
    <g transform={`translate(${x} ${y}) scale(${scale} ${scale * LOOKING_DOWN_SQUASH})`}>
      <defs>
        <radialGradient id="chairShadow">
          <stop offset="0" stopColor="#000" stopOpacity="0.3" />
          <stop offset="0.6" stopColor="#000" stopOpacity="0.14" />
          <stop offset="1" stopColor="#000" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="chairTyre" x1="0" x2="1">
          <stop offset="0" stopColor="#0c0d0f" />
          <stop offset="0.45" stopColor="#34373c" />
          <stop offset="1" stopColor="#0c0d0f" />
        </linearGradient>
        <linearGradient id="chairBack" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#4b5059" />
          <stop offset="0.5" stopColor="#2c2f35" />
          <stop offset="1" stopColor="#17191c" />
        </linearGradient>
        <linearGradient id="chairSheen" x1="0" x2="1">
          <stop offset="0" stopColor="#fff" stopOpacity="0.12" />
          <stop offset="0.35" stopColor="#fff" stopOpacity="0" />
          <stop offset="1" stopColor="#000" stopOpacity="0.18" />
        </linearGradient>
        <linearGradient id="chairFrame" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#3a3d43" />
          <stop offset="1" stopColor="#1a1c1f" />
        </linearGradient>
        <radialGradient id="chairLightGlow">
          <stop offset="0" stopColor="#ff5a52" stopOpacity="0.55" />
          <stop offset="1" stopColor="#ff5a52" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="chairMetal" x1="0" x2="1">
          <stop offset="0" stopColor="#6d727b" />
          <stop offset="0.5" stopColor="#c9ccd2" />
          <stop offset="1" stopColor="#6d727b" />
        </linearGradient>
      </defs>

      <ellipse cx="0" cy="3" rx="74" ry="17" fill="url(#chairShadow)" />

      {/* anti-tip wheels */}
      <rect x="-25" y="-12" width="8" height="15" rx="4" fill="#111214" />
      <rect x="17" y="-12" width="8" height="15" rx="4" fill="#111214" />

      {/* drive wheels: tyre, tread, hub */}
      {[-1, 1].map((side) => (
        <g key={side} transform={`translate(${side * 44} 0)`}>
          <rect x="-9" y="-72" width="18" height="76" rx="9" fill="url(#chairTyre)" />
          <g stroke="#000" strokeOpacity="0.45" strokeWidth="1.2">
            {[-58, -48, -38, -28, -18, -8].map((y) => (
              <line key={y} x1="-7" y1={y} x2="7" y2={y - 3 * side} />
            ))}
          </g>
          <rect x={side * -11 - 3} y="-46" width="6" height="22" rx="3" fill="url(#chairMetal)" opacity="0.85" />
        </g>
      ))}

      {/* axle and lower frame */}
      <rect x="-40" y="-37" width="80" height="6" rx="3" fill="url(#chairMetal)" opacity="0.7" />
      <rect x="-32" y="-58" width="64" height="40" rx="8" fill="url(#chairFrame)" />
      <rect x="-32" y="-58" width="64" height="40" rx="8" fill="url(#chairSheen)" />

      {/* tail lights */}
      {[-1, 1].map((side) => (
        <g key={side}>
          <ellipse cx={side * 18} cy="-28" rx="17" ry="9" fill="url(#chairLightGlow)" />
          <rect x={side * 18 - 9} y="-31" width="18" height="6" rx="3" fill="var(--viz-edge)" />
          <rect x={side * 18 - 6} y="-30" width="12" height="1.5" rx="0.75" fill="#fff" opacity="0.55" />
        </g>
      ))}

      {/* armrests, reaching forward past the backrest */}
      <rect x="-42" y="-106" width="10" height="26" rx="5" fill="#202327" />
      <rect x="32" y="-106" width="10" height="26" rx="5" fill="#202327" />
      <rect x="-41" y="-105" width="8" height="3" rx="1.5" fill="#fff" opacity="0.14" />
      <rect x="33" y="-105" width="8" height="3" rx="1.5" fill="#fff" opacity="0.14" />

      {/* joystick on the right armrest */}
      <rect x="35.5" y="-114" width="3" height="9" rx="1.5" fill="url(#chairMetal)" />
      <circle cx="37" cy="-116" r="4.5" fill="#16181b" />
      <circle cx="35.8" cy="-117.2" r="1.4" fill="#fff" opacity="0.35" />

      {/* backrest uprights and push handles */}
      <rect x="-34" y="-140" width="5" height="88" rx="2.5" fill="url(#chairMetal)" opacity="0.8" />
      <rect x="29" y="-140" width="5" height="88" rx="2.5" fill="url(#chairMetal)" opacity="0.8" />
      <rect x="-37" y="-150" width="11" height="20" rx="5.5" fill="#121315" />
      <rect x="26" y="-150" width="11" height="20" rx="5.5" fill="#121315" />
      <rect x="-35.5" y="-148" width="2.5" height="14" rx="1.25" fill="#fff" opacity="0.16" />
      <rect x="27.5" y="-148" width="2.5" height="14" rx="1.25" fill="#fff" opacity="0.16" />

      {/* backrest */}
      <rect x="-30" y="-136" width="60" height="84" rx="13" fill="url(#chairBack)" />
      <rect x="-30" y="-136" width="60" height="84" rx="13" fill="url(#chairSheen)" />
      <rect x="-23" y="-128" width="46" height="68" rx="8" fill="none" stroke="#fff" strokeOpacity="0.09" strokeWidth="1.5" />
      <path d="M-23 -96h46" stroke="#000" strokeOpacity="0.28" strokeWidth="1.5" />
      <path d="M-23 -94.5h46" stroke="#fff" strokeOpacity="0.07" strokeWidth="1" />
      <rect x="-20" y="-134" width="40" height="3" rx="1.5" fill="#fff" opacity="0.2" />
    </g>
  )
}
