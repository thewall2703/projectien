import DataTable from "./DataTable";

export default function Objections() {
  return (
    <DataTable
      title="Objections"
      path="/api/admin/objections"
      fields={[
        { key: "question", label: "Question", type: "textarea" },
        { key: "who_asks", label: "Who asks" },
        { key: "move", label: "Move", type: "textarea" },
        { key: "answer", label: "Answer", type: "textarea" },
        { key: "status", label: "Status", type: "select", options: ["approved", "blocking"] },
      ]}
    />
  );
}
