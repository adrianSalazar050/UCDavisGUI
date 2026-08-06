import { useState } from "react";
import AddPrinterForm from "../components/printers/AddPrinterForm.jsx";
import PrinterCard from "../components/printers/PrinterCard.jsx";
import Button from "../components/ui/Button.jsx";
import EmptyState from "../components/ui/EmptyState.jsx";
import PageFrame from "../components/ui/PageFrame.jsx";
import Section from "../components/ui/Section.jsx";

// The fleet's setup screen: register a machine, correct what is stored about
// it, retire it. Choosing which printer the rest of the app shows moved to the
// topbar switcher, so this page is no longer the route to anywhere.
//
// Nothing here repeats the page title or the fleet tally. The topbar already
// carries both -- "Printers" and "2 printers · 1 online" -- and a third copy
// on the section heading read as a third fact to reconcile rather than as the
// same one said again.
export default function Printers({ printers, selected, onSelect }) {
  // Registering a printer is a once-per-machine job, so the form is collapsed:
  // it used to own the bottom half of the page for the rest of the fleet's
  // life. Collapsing unmounts it, which is also the cancel -- a half-typed
  // address is abandoned on purpose rather than stashed.
  const [adding, setAdding] = useState(false);

  // aria-expanded with no aria-controls: the panel does not exist while
  // collapsed, so there is no id to point at.
  const reveal = (
    <Button variant={adding ? "secondary" : "primary"} aria-expanded={adding}
            onClick={() => setAdding((v) => !v)}>
      {adding ? "Cancel" : "Add a printer"}
    </Button>
  );

  // Exactly one reveal control renders at a time, in one of two homes: with
  // nothing registered the empty state IS the page and owns it, otherwise it
  // sits above the form it opens.
  const showPanel = adding || printers.length > 0;

  return (
    <PageFrame>
      {printers.length > 0 && (
        <div className="printer-grid">
          {printers.map((p) => (
            <PrinterCard key={p.serial} printer={p}
                         selected={p.serial === selected}
                         onSelect={onSelect} />
          ))}
        </div>
      )}
      {/* The empty state steps aside once the form it revealed is open: a
          "no printers yet" panel captioning the form that fixes exactly that
          is furniture, not information. */}
      {printers.length === 0 && !adding && (
        <EmptyState title="No printers registered yet" action={reveal}>
          Registering one takes its IP address, serial and LAN access code —
          all three are on the printer’s own screen.
        </EmptyState>
      )}
      {showPanel && (
        <Section>
          {/* .row, not the bare Button: .ui-section stretches its children, and
              a full-width primary button reads as the whole page's action. */}
          <div className="row">{reveal}</div>
          {adding && <AddPrinterForm />}
        </Section>
      )}
    </PageFrame>
  );
}
