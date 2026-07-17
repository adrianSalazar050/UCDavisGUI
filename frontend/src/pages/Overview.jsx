import AddPrinterForm from "../components/printers/AddPrinterForm.jsx";
import PrinterCard from "../components/printers/PrinterCard.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Section from "../components/ui/Section.jsx";

export default function Overview({ printers, selected, onSelect }) {
  const online = printers.filter((p) => p.connection === "ok").length;
  const title = printers.length === 0
    ? "Printers"
    : `Printers (${printers.length} · ${online} online)`;

  return (
    <PageFrame>
      <Section title={title}>
        {printers.length === 0 ? (
          <div className="empty">
            No printers yet — add one below to start monitoring it.
          </div>
        ) : (
          <div className="printer-grid">
            {printers.map((p) => (
              <PrinterCard key={p.serial} printer={p}
                           selected={p.serial === selected}
                           onSelect={onSelect} />
            ))}
          </div>
        )}
      </Section>
      <Section title="Add printer">
        <AddPrinterForm />
      </Section>
    </PageFrame>
  );
}
