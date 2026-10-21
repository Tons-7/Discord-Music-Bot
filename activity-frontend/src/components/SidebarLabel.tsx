/** The sidebar's own tooltip: shown on hover and on keyboard focus of its button (the button is the group). */
export function SidebarLabel({ children }: { children: React.ReactNode }) {
  return (
    <span
      aria-hidden="true"
      className="absolute right-full mr-2 z-50 flex items-center gap-1.5 px-2 py-1 rounded-lg bg-surface-3 text-xs text-fg font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 pointer-events-none transition-opacity shadow-lg"
    >
      {children}
    </span>
  );
}
