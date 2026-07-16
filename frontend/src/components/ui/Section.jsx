export default function Section({ title, children }) {
  return (
    <section className="ui-section">
      {title && <h2 className="ui-section__title">{title}</h2>}
      {children}
    </section>
  );
}
