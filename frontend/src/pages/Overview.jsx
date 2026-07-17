import PageFrame from "../components/ui/PageFrame.jsx";

export default function Overview({ printers }) {
  return (
    <PageFrame>
      <pre>{JSON.stringify(printers, null, 2)}</pre>
    </PageFrame>
  );
}
