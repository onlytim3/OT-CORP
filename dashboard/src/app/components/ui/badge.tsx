import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "./utils";

const badgeVariants = cva(
  "inline-flex items-center justify-center rounded-md border px-2 py-0.5 text-xs font-medium w-fit whitespace-nowrap shrink-0 [&>svg]:size-3 gap-1 [&>svg]:pointer-events-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive transition-[color,box-shadow] overflow-hidden backdrop-blur-sm",
  {
    variants: {
      variant: {
        default:
          "border-[#4a9eff]/30 bg-[#4a9eff]/10 text-[#4a9eff] [a&]:hover:bg-[#4a9eff]/20",
        secondary:
          "border-white/20 bg-white/5 text-[#c0c0c0] [a&]:hover:bg-white/10",
        destructive:
          "border-[#ff4466]/30 bg-[#ff4466]/10 text-[#ff4466] [a&]:hover:bg-[#ff4466]/20",
        outline:
          "border-white/20 text-[#e8e8e8] [a&]:hover:bg-white/5",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Badge({
  className,
  variant,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : "span";

  return (
    <Comp
      data-slot="badge"
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  );
}

export { Badge, badgeVariants };