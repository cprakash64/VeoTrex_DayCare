"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

/** Marks the active destination for assistive technology as well as for the eye. */
export function NavLink({ href, children }: { href: string; children: ReactNode }) {
  const pathname = usePathname();
  const active = href === "/app" ? pathname === "/app" : pathname.startsWith(href);
  return (
    <a className="app-nav__link" href={href} aria-current={active ? "page" : undefined}>
      {children}
    </a>
  );
}
