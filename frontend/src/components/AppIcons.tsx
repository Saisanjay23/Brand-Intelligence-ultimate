import React from "react";

export interface IconProps {
  size?: number;
  color?: string;
  className?: string;
  style?: React.CSSProperties;
}

export function SunIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <circle cx="12" cy="12" r="5" />
      <line x1="12" y1="1" x2="12" y2="3" />
      <line x1="12" y1="21" x2="12" y2="23" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
      <line x1="1" y1="12" x2="3" y2="12" />
      <line x1="21" y1="12" x2="23" y2="12" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
    </svg>
  );
}

export function MoonIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

/**
 * Official Verified Blue Tick Badge — matches real social platform verification badges.
 * Filled blue shield/circle with a white checkmark inside.
 */
export function VerifiedBadgeIcon({ size = 16, color = "#1D9BF0", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
      aria-label="Verified"
    >
      <path
        d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81C14.67 2.63 13.43 1.75 12 1.75S9.33 2.63 8.66 3.94c-1.39-.46-2.9-.2-3.91.81S3.48 7.27 3.94 8.66C2.63 9.33 1.75 10.57 1.75 12s.88 2.67 2.19 3.34c-.46 1.39-.2 2.9.81 3.91s2.52 1.27 3.91.81c.67 1.31 1.91 2.19 3.34 2.19s2.67-.88 3.34-2.19c1.39.46 2.9.2 3.91-.81s1.27-2.52.81-3.91c1.31-.67 2.19-1.91 2.19-3.34Z"
        fill={color}
      />
      <path
        d="m9.5 12 2 2 4-4.5"
        stroke="#fff"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Custom Brand Intelligence Suite Brand Logo Mark
 */
export function BrandLogoIcon({ size = 28, color, className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <defs>
        <linearGradient id="brandLogoGrad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#00F0FF" />
          <stop offset="50%" stopColor="#7C5CFF" />
          <stop offset="100%" stopColor="#4F46E5" />
        </linearGradient>
      </defs>
      <path
        d="M16 3L26 7.5V14.5C26 21.2 21.7 27.2 16 29.5C10.3 27.2 6 21.2 6 14.5V7.5L16 3Z"
        fill="url(#brandLogoGrad)"
        fillOpacity="0.22"
        stroke="url(#brandLogoGrad)"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="16" cy="16" r="6" stroke="#00F0FF" strokeWidth="1.2" strokeDasharray="2 2" strokeOpacity="0.8" />
      <circle cx="16" cy="16" r="2" fill="#00F0FF" />
      <line x1="16" y1="8" x2="16" y2="24" stroke="#7C5CFF" strokeWidth="1.2" strokeLinecap="round" strokeOpacity="0.7" />
      <line x1="8" y1="16" x2="24" y2="16" stroke="#7C5CFF" strokeWidth="1.2" strokeLinecap="round" strokeOpacity="0.7" />
    </svg>
  );
}

/**
 * Custom Discover Sweep Radar Icon
 */
export function DiscoverIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="12" r="9" stroke={color} strokeWidth="1.75" strokeOpacity="0.8" />
      <circle cx="12" cy="12" r="5" stroke={color} strokeWidth="1.5" strokeOpacity="0.5" />
      <circle cx="12" cy="12" r="1.75" fill={color} />
      <path
        d="M12 12L18.5 5.5"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M12 3A9 9 0 0 1 21 12"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeOpacity="0.9"
      />
    </svg>
  );
}

/**
 * Custom Threat Deep Analysis & Audit Icon
 */
export function AnalyseIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M12 3L20 7V13C20 17.5 16.5 21.5 12 22.5C7.5 21.5 4 17.5 4 13V7L12 3Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeOpacity="0.6"
      />
      <path
        d="M9 12L11 14L15 10"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="12" r="6" stroke={color} strokeWidth="1.5" strokeDasharray="2 2" />
    </svg>
  );
}

/**
 * Custom Clients Organization Shield Icon
 */
export function ClientsNavIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M3 21H21"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
      />
      <path
        d="M5 21V7L13 3V21"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M13 10L19 12V21"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line x1="8" y1="10" x2="10" y2="10" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <line x1="8" y1="14" x2="10" y2="14" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <line x1="16" y1="14" x2="17" y2="14" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Live Results & Telemetry Icon
 */
export function LiveResultsNavIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="12" r="9" stroke={color} strokeWidth="1.75" strokeOpacity="0.5" />
      <path
        d="M3.5 12C6 8 8 7 12 12C16 17 18 16 20.5 12"
        stroke={color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="12" r="2.5" fill={color} />
    </svg>
  );
}

/**
 * Custom Admin Command Center Icon
 */
export function AdminNavIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="12" r="3" stroke={color} strokeWidth="1.75" />
      <path
        d="M19.4 15A1.65 1.65 0 0 0 19.73 16.82L20.2 17.63A2 2 0 0 1 18.5 20.6L17.57 20.07A1.65 1.65 0 0 0 15.35 20.73L14.88 21.55A2 2 0 0 1 11.4 21.55L10.93 20.73A1.65 1.65 0 0 0 8.71 20.07L7.78 20.6A2 2 0 0 1 6.08 17.63L6.55 16.82A1.65 1.65 0 0 0 6.22 15L5.3 14.47A2 2 0 0 1 5.3 10.99L6.22 10.46A1.65 1.65 0 0 0 6.55 8.64L6.08 7.83A2 2 0 0 1 7.78 4.86L8.71 5.39A1.65 1.65 0 0 0 10.93 4.73L11.4 3.91A2 2 0 0 1 14.88 3.91L15.35 4.73A1.65 1.65 0 0 0 17.57 5.39L18.5 4.86A2 2 0 0 1 20.2 7.83L19.73 8.64A1.65 1.65 0 0 0 19.4 10.46L20.32 10.99A2 2 0 0 1 20.32 14.47L19.4 15Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Custom Notification Bell / Alert Beacon
 */
export function BellAlertIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M18 8A6 6 0 0 0 6 8C6 15 3 17 3 17H21S18 15 18 8Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M13.73 21A2 2 0 0 1 10.27 21"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Custom Cyber Grid Globe (For All Platforms / Domains / Web)
 */
export function CyberGlobeIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="12" r="9" stroke={color} strokeWidth="1.75" />
      <ellipse cx="12" cy="12" rx="4" ry="9" stroke={color} strokeWidth="1.5" strokeOpacity="0.75" />
      <line x1="3" y1="12" x2="21" y2="12" stroke={color} strokeWidth="1.5" strokeOpacity="0.75" />
      <line x1="4.5" y1="7" x2="19.5" y2="7" stroke={color} strokeWidth="1.25" strokeOpacity="0.5" />
      <line x1="4.5" y1="17" x2="19.5" y2="17" stroke={color} strokeWidth="1.25" strokeOpacity="0.5" />
    </svg>
  );
}

export const GlobeIcon = CyberGlobeIcon;

/**
 * Custom Target / Crosshairs / Limits Icon (Replaces 🎯)
 */
export function TargetIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="12" r="9" stroke={color} strokeWidth="1.75" />
      <circle cx="12" cy="12" r="5" stroke={color} strokeWidth="1.5" strokeOpacity="0.7" />
      <circle cx="12" cy="12" r="1.75" fill={color} />
      <line x1="12" y1="1" x2="12" y2="4" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
      <line x1="12" y1="20" x2="12" y2="23" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
      <line x1="1" y1="12" x2="4" y2="12" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
      <line x1="20" y1="12" x2="23" y2="12" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom User / Individual Profile Icon (Replaces 👤)
 */
export function UserIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="7" r="4" stroke={color} strokeWidth="1.75" />
      <path
        d="M4.5 20C4.5 16.134 7.85786 13 12 13C16.1421 13 19.5 16.134 19.5 20"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
      />
    </svg>
  );
}

/**
 * Custom Tag / Keyword Icon (Replaces 🏷️)
 */
export function TagIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M20.59 13.41L13.42 20.58A2 2 0 0 1 12 21.17A2 2 0 0 1 10.59 20.59L3.41 13.41A2 2 0 0 1 2.83 12V4A1.17 1.17 0 0 1 4 2.83H12A2 2 0 0 1 13.41 3.41L20.59 10.59A2 2 0 0 1 20.59 13.41Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="7.5" cy="7.5" r="1.5" fill={color} />
    </svg>
  );
}

/**
 * Custom Disk / Save Icon (Replaces 💾)
 */
export function SaveIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M19 21H5A2 2 0 0 1 3 19V5A2 2 0 0 1 5 3H16L21 8V19A2 2 0 0 1 19 21Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M17 21V13H7V21" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M7 3V7H14V3" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/**
 * Custom AI Sparkles / Generator Icon (Replaces ✨)
 */
export function SparklesIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M12 2L13.8 8.2L20 10L13.8 11.8L12 18L10.2 11.8L4 10L10.2 8.2L12 2Z"
        fill={color}
        fillOpacity="0.25"
        stroke={color}
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M19 16L19.8 18.2L22 19L19.8 19.8L19 22L18.2 19.8L16 19L18.2 18.2L19 16Z"
        fill={color}
        stroke={color}
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Custom Building / Organization Icon (Replaces 🏢)
 */
export function BuildingIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path d="M3 21H21" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
      <path
        d="M5 21V4C5 3.44772 5.44772 3 6 3H14C14.5523 3 15 3.44772 15 4V21"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M15 9H18C18.5523 9 19 9.44772 19 10V21"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line x1="8" y1="7" x2="10" y2="7" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <line x1="8" y1="11" x2="10" y2="11" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <line x1="8" y1="15" x2="10" y2="15" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Search Magnifier Icon (Replaces 🔍 / 🔎)
 */
export function SearchIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="11" cy="11" r="7" stroke={color} strokeWidth="1.75" />
      <line x1="16.5" y1="16.5" x2="21.5" y2="21.5" stroke={color} strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Plus / Add Icon (Replaces ➕)
 */
export function PlusIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <line x1="12" y1="5" x2="12" y2="19" stroke={color} strokeWidth="2" strokeLinecap="round" />
      <line x1="5" y1="12" x2="19" y2="12" stroke={color} strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Trash / Delete Icon (Replaces 🗑️)
 */
export function TrashIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path d="M3 6H21" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
      <path
        d="M19 6V19C19 20.1046 18.1046 21 17 21H7C5.89543 21 5 20.1046 5 19V6"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M8 6V4C8 3.44772 8.44772 3 9 3H15C15.5523 3 16 3.44772 16 4V6"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line x1="10" y1="11" x2="10" y2="17" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <line x1="14" y1="11" x2="14" y2="17" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Edit / Pencil Icon (Replaces ✏️)
 */
export function EditIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M14.7 3.58a1 1 0 0 1 1.4 0l4.32 4.32a1 1 0 0 1 0 1.4l-12 12a1 1 0 0 1-.44.26l-5.32 1.33a.5.5 0 0 1-.6-.6l1.33-5.32a1 1 0 0 1 .26-.44l12.05-12.05z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line x1="13.5" y1="4.78" x2="18.8" y2="10.08" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Clone / Clipboard Icon (Replaces 📋)
 */
export function CloneIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <rect x="8" y="8" width="13" height="13" rx="2" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      <path
        d="M5 15H4C3.44772 15 3 14.5523 3 14V4C3 3.44772 3.44772 3 4 3H14C14.5523 3 15 3.44772 15 4V5"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export const CopyIcon = CloneIcon;

/**
 * Custom Lightning Bolt / Zap Icon (Replaces ⚡)
 */
export function ZapIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <polygon
        points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill={color}
        fillOpacity="0.2"
      />
    </svg>
  );
}

/**
 * Custom Settings Gear Icon (Replaces ⚙️)
 */
export function SettingsGearIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="12" r="3" stroke={color} strokeWidth="1.75" />
      <path
        d="M19.4 15A1.65 1.65 0 0 0 19.73 16.82L20.2 17.63A2 2 0 0 1 18.5 20.6L17.57 20.07A1.65 1.65 0 0 0 15.35 20.73L14.88 21.55A2 2 0 0 1 11.4 21.55L10.93 20.73A1.65 1.65 0 0 0 8.71 20.07L7.78 20.6A2 2 0 0 1 6.08 17.63L6.55 16.82A1.65 1.65 0 0 0 6.22 15L5.3 14.47A2 2 0 0 1 5.3 10.99L6.22 10.46A1.65 1.65 0 0 0 6.55 8.64L6.08 7.83A2 2 0 0 1 7.78 4.86L8.71 5.39A1.65 1.65 0 0 0 10.93 4.73L11.4 3.91A2 2 0 0 1 14.88 3.91L15.35 4.73A1.65 1.65 0 0 0 17.57 5.39L18.5 4.86A2 2 0 0 1 20.2 7.83L19.73 8.64A1.65 1.65 0 0 0 19.4 10.46L20.32 10.99A2 2 0 0 1 20.32 14.47L19.4 15Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Custom Alert Warning Triangle (Replaces ⚠️ / ⚠)
 */
export function AlertTriangleIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M10.29 3.86L1.82 18A2 2 0 0 0 3.55 21H20.45A2 2 0 0 0 22.18 18L13.71 3.86A2 2 0 0 0 10.29 3.86Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line x1="12" y1="9" x2="12" y2="13" stroke={color} strokeWidth="2" strokeLinecap="round" />
      <circle cx="12" cy="17" r="1" fill={color} />
    </svg>
  );
}

/**
 * Custom Layers / Asset Icon (Replaces 🏷️ Asset Names)
 */
export function LayersIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <polygon points="12 2 2 7 12 12 22 7 12 2" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2 17L12 22L22 17" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2 12L12 17L22 12" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/**
 * Custom Clock / Timer Icon (Replaces ⏱️ / ⏱)
 */
export function ClockIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="12" r="9" stroke={color} strokeWidth="1.75" />
      <polyline points="12 7 12 12 15.5 14" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/**
 * Custom Refresh / Sync Icon (Replaces 🔄)
 */
export function RefreshIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path d="M23 4V10H17" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M1 20V14H7" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      <path
        d="M3.51 9A9 9 0 0 1 20.49 5.51L23 10M1 14L3.51 18.49A9 9 0 0 0 20.49 15"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Custom Lock Icon (Replaces 🔒)
 */
export function LockIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <rect x="3" y="11" width="18" height="11" rx="2" stroke={color} strokeWidth="1.75" />
      <path d="M7 11V7A5 5 0 0 1 17 7V11" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Unlock Icon (Replaces 🔓)
 */
export function UnlockIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <rect x="3" y="11" width="18" height="11" rx="2" stroke={color} strokeWidth="1.75" />
      <path d="M7 11V7A5 5 0 0 1 17 7" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Shield Icon (Replaces 🛡️)
 */
export function ShieldIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M12 22C12 22 20 18 20 12V5L12 2L4 5V12C4 18 12 22 12 22Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Custom Play Icon (Replaces ▶)
 */
export function PlayIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <polygon points="5 3 19 12 5 21 5 3" fill={color} />
    </svg>
  );
}

/**
 * Custom Pause Icon (Replaces ⏸)
 */
export function PauseIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <rect x="6" y="4" width="4" height="16" rx="1" fill={color} />
      <rect x="14" y="4" width="4" height="16" rx="1" fill={color} />
    </svg>
  );
}

/**
 * Custom Database / Pool Icon (Replaces 🗃️ / 🗄)
 */
export function DatabaseIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <ellipse cx="12" cy="5" rx="9" ry="3" stroke={color} strokeWidth="1.75" />
      <path d="M21 12C21 13.66 16.97 15 12 15C7.03 15 3 13.66 3 12" stroke={color} strokeWidth="1.75" />
      <path d="M3 5V19C3 20.66 7.03 22 12 22C16.97 22 21 20.66 21 19V5" stroke={color} strokeWidth="1.75" />
    </svg>
  );
}

/**
 * Custom Filter Funnel Icon (Replaces Filter icon)
 */
export function FilterIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/**
 * Custom Chemistry / Test Flask Icon (Replaces 🧪)
 */
export function FlaskIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M10 2V8L4.72 17.5C4.05 18.7 4.92 20.2 6.28 20.2H17.72C19.08 20.2 19.95 18.7 19.28 17.5L14 8V2H10Z"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line x1="8.5" y1="2" x2="15.5" y2="2" stroke={color} strokeWidth="1.75" strokeLinecap="round" />
      <line x1="6.5" y1="15" x2="17.5" y2="15" stroke={color} strokeWidth="1.5" strokeDasharray="2 2" />
    </svg>
  );
}

/**
 * Custom Session Credentials Key Icon
 */
export function SessionsKeyIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="7.5" cy="15.5" r="4.5" stroke={color} strokeWidth="1.75" />
      <path
        d="M10.8 12.2L19.5 3.5M16 7L18.5 9.5M18.5 4.5L20.5 6.5"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export const KeyIcon = SessionsKeyIcon;

/**
 * Custom Mail Alerts Envelope Icon
 */
export function MailAlertIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <rect x="3" y="5" width="18" height="14" rx="3" stroke={color} strokeWidth="1.75" />
      <path
        d="M3 7L12 13L21 7"
        stroke={color}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export const MailIcon = MailAlertIcon;

/**
 * Custom Network Proxy Nodes Icon
 */
export function ProxyNodeIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="5" cy="6" r="3" stroke={color} strokeWidth="1.75" />
      <circle cx="19" cy="6" r="3" stroke={color} strokeWidth="1.75" />
      <circle cx="12" cy="18" r="3" stroke={color} strokeWidth="1.75" />
      <path
        d="M7.5 7.5L10 15.5M16.5 7.5L14 15.5M8 6H16"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeDasharray="2 2"
      />
    </svg>
  );
}

/**
 * Custom Scheduler Clock Cycle Icon
 */
export function SchedulerClockIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <circle cx="12" cy="12" r="9" stroke={color} strokeWidth="1.75" />
      <polyline points="12 7 12 12 15.5 14" stroke={color} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/**
 * Custom Live Activity Waveform Icon
 */
export function ActivityWaveIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M2 12H6L9 4L15 20L18 12H22"
        stroke={color}
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Custom Abort / Stop Action Icon
 */
export function StopIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <rect x="5" y="5" width="14" height="14" rx="3" fill={color} />
    </svg>
  );
}

/**
 * Custom Download / Export Action Icon
 */
export function DownloadIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path d="M21 15V19C21 20.1046 20.1046 21 19 21H5C3.89543 21 3 20.1046 3 19V15" stroke={color} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M7 10L12 15L17 10" stroke={color} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <line x1="12" y1="15" x2="12" y2="3" stroke={color} strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Custom Analysis Lightning / Sparkle Nav Icon
 */
export function AnalysisNavIcon({ size = 16, color = "currentColor", className, style }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
    >
      <path
        d="M13 2L3 14H12L11 22L21 10H12L13 2Z"
        stroke={color}
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="currentColor"
        fillOpacity="0.15"
      />
    </svg>
  );
}

