export default function Button({ variant = "secondary", size = "md",
                                 busy = false, children, ...rest }) {
  const cls = ["ui-btn", `ui-btn--${variant}`];
  if (size === "sm") cls.push("ui-btn--sm");
  return (
    <button className={cls.join(" ")} disabled={busy || rest.disabled} {...rest}>
      {busy ? "…" : children}
    </button>
  );
}
