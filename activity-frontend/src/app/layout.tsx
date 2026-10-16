import type { Metadata, Viewport } from "next";
import { APPLY_SAVED_THEME } from "@/lib/themes";
import "./globals.css";

export const metadata: Metadata = {
  title: "Music Bot",
  description: "Discord Music Bot Activity",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // The theme script sets data-theme before hydration.
    <html lang="en" className="h-full" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: APPLY_SAVED_THEME }} />
      </head>
      <body className="h-full">{children}</body>
    </html>
  );
}
