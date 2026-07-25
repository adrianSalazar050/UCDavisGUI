// Bambu model ids as they appear in a sliced .gcode.3mf
// (Metadata/slice_info.config -> printer_model_id). Mirrors MODEL_NAMES in
// server/store.py; the server holds the authoritative copy and does the
// actual comparison, this list only populates the dropdown.
//
// VERIFIED: "N2S" = A1 (this repo's printer, 2026-07-21). The rest is
// community knowledge and unconfirmed -- which is safe, because an id the
// user picks wrongly can only cause a refused start, never a bad print, and
// "Unknown" always disables the check.
export const PRINTER_MODELS = [
  { id: "", name: "Unknown (no check)" },
  { id: "N2S", name: "A1" },
  { id: "N1", name: "A1 mini" },
  { id: "C11", name: "P1P" },
  { id: "C12", name: "P1S" },
  { id: "BL-P001", name: "X1 Carbon" },
  { id: "BL-P002", name: "X1" },
];

export const modelName = (id) =>
  PRINTER_MODELS.find((m) => m.id === id)?.name ?? id;

// Same prefix table as server/store.py's SERIAL_PREFIX_MODELS, used only to
// preselect the dropdown for a printer that has no model set yet.
const SERIAL_PREFIX_MODELS = { "039": "N2S", "030": "N1" };

export const guessModelId = (serial) => {
  if (typeof serial !== "string") return "";
  const hit = Object.entries(SERIAL_PREFIX_MODELS)
    .find(([prefix]) => serial.startsWith(prefix));
  return hit ? hit[1] : "";
};

// Mirrors BED_TYPES in server/store.py exactly -- the server validates
// against that list and degrades anything else to DEFAULT_BED_TYPE, this
// list only populates the dropdown. Unlike PRINTER_MODELS there is no
// "Unknown" entry: every printer has a plate actually installed, and a
// wrong/missing curr_bed_type is the real, measured defect this field
// exists to fix (Cool Plate's 35 C vs the Textured PEI Plate's 65 C this
// lab's A1 needs for PLA -- see PrinterConfig.bed_type in server/store.py).
export const BED_TYPES = [
  "Cool Plate",
  "Textured PEI Plate",
  "High Temp Plate",
  "Engineering Plate",
  // "Supertack Plate", not the "Cool Plate (SuperTack)" marketing name --
  // the slicer silently treats an unrecognised plate as Cool Plate (35 C),
  // so a wrong string here is a wrong bed temperature with no error anywhere.
  "Supertack Plate",
];

// Mirrors NOZZLES in server/store.py exactly -- the server validates
// against that tuple and degrades anything else to DEFAULT_NOZZLE, this
// list only populates the dropdown. Like BED_TYPES there is no "Unknown"
// entry: every printer has a nozzle actually installed, and the wrong
// diameter here selects the wrong machine profile when slicing (see
// PrinterConfig.nozzle in server/store.py).
export const NOZZLES = ["0.2", "0.4", "0.6", "0.8"];
