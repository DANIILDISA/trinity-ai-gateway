type SpinnerProps = {
  size?: "sm" | "md" | "lg";
  className?: string;
};

const SIZE: Record<NonNullable<SpinnerProps["size"]>, string> = {
  sm: "h-3 w-3 border-2",
  md: "h-4 w-4 border-2",
  lg: "h-6 w-6 border-[3px]",
};

export function Spinner({ size = "md", className = "" }: SpinnerProps) {
  return (
    <span
      role="status"
      aria-label="Загрузка"
      className={`inline-block animate-spin rounded-full border-current border-t-transparent ${SIZE[size]} ${className}`}
    />
  );
}

export default Spinner;
