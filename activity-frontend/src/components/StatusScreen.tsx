import { AlertIcon } from "./ui/icons";

/** Full-screen message for startup and connection states that leave nothing else to show. */
export default function StatusScreen({ kind, message, action, footer }: {
  kind: "loading" | "error" | "info";
  message: string;
  action?: { label: string; onClick: () => void };
  footer?: React.ReactNode;
}) {
  return (
    <div className="h-dvh flex flex-col items-center justify-center bg-surface-1 gap-4 px-8 text-center" role={kind === "error" ? "alert" : "status"}>
      {kind === "loading" && <div className="w-10 h-10 border-2 border-accent border-t-transparent rounded-full animate-spin" aria-hidden="true" />}
      {kind === "error" && (
        <div className="w-16 h-16 rounded-2xl bg-danger/10 flex items-center justify-center">
          <AlertIcon className="w-8 h-8 text-danger" strokeWidth={1.5} />
        </div>
      )}
      <p className="text-sm text-muted max-w-[280px]">{message}</p>
      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className="px-5 py-2 bg-accent hover:bg-accent/80 text-white rounded-full text-sm font-medium transition-all active:scale-95"
        >
          {action.label}
        </button>
      )}
      {footer}
    </div>
  );
}
