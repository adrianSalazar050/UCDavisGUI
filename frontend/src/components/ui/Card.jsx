export default function Card({ title, children, className = "" }) {
  return (
    <div className={`ui-card ${className}`.trim()}>
      {title && <h3 className="ui-card__title">{title}</h3>}
      {children}
    </div>
  );
}
