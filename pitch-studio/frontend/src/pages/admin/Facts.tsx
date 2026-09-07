import DataTable from "./DataTable";

export default function Facts() {
  return (
    <DataTable
      title="Locked facts"
      path="/api/admin/facts"
      fields={[
        { key: "fact", label: "Fact" },
        { key: "value", label: "Value", type: "textarea" },
        { key: "source", label: "Source" },
        {
          key: "status",
          label: "Status",
          type: "select",
          options: ["verified", "needs_source", "conflict", "do_not_use", "needs_decision"],
        },
        { key: "note", label: "Note", type: "textarea" },
        { key: "module_ids", label: "Module IDs" },
      ]}
    />
  );
}
