export default function Button({ variant = "secondary", size = "md",
                                 busy = false, disabled = false,
                                 type = "button", className = "",
                                 children, ...rest }) {
  // disabled/type/className are pulled out of props explicitly, NOT left in
  // ...rest. Spreading rest last (needed so a caller's onClick etc. land) used
  // to let a caller's `disabled` clobber `busy || disabled` — a busy button
  // stayed clickable. Destructuring makes the merge authoritative while `type`
  // and `className` stay overridable/additive.
  const cls = ["ui-btn", `ui-btn--${variant}`];
  if (size === "sm") cls.push("ui-btn--sm");
  if (className) cls.push(className);
  return (
    <button type={type} className={cls.join(" ")}
            disabled={busy || disabled} {...rest}>
      {busy ? "…" : children}
    </button>
  );
}
