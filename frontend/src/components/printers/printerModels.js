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
