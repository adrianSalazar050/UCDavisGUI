import PageFrame from "../components/ui/PageFrame.jsx";

export default function SdFiles({ selected }) {
  return <PageFrame><div className="empty">SD Files for {selected ?? "—"}</div></PageFrame>;
}
