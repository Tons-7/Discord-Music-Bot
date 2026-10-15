export default function KeyCap({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="min-w-6 h-6 px-1.5 inline-flex items-center justify-center rounded-md border border-white/[0.15] border-b-2 bg-white/[0.04] font-sans text-xs text-white/80">
      {children}
    </kbd>
  );
}
