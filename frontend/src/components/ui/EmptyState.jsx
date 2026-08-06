// The shared "there is nothing here yet" panel.
//
// Every page had its own bare <div className="empty">…</div>, and half of them
// told the reader to go and do something -- five said "pick one on the Overview
// page", naming a page that no longer exists under that name and an errand the
// topbar switcher had made unnecessary -- without giving them any way to do it
// from where they stood. This keeps the same `.empty` look and
// adds the two things that make such a message actionable: a headline that
// says WHAT is missing, and an optional button that goes and fixes it.
export default function EmptyState({ title, children, action }) {
  return (
    <div className="empty">
      {title && <div className="empty__title">{title}</div>}
      {children && <div className="empty__body">{children}</div>}
      {action && <div className="empty__action">{action}</div>}
    </div>
  );
}
