export default function NavGroup({ label, items, activeKey, onSelect }) {
  return (
    <nav className="ui-navgroup">
      <div className="ui-navgroup__label">{label}</div>
      {items.map(({ key, title }) => (
        <button
          key={key}
          className={`ui-navgroup__item${key === activeKey ? " ui-navgroup__item--active" : ""}`}
          onClick={() => onSelect(key)}
        >
          {title}
        </button>
      ))}
    </nav>
  );
}
