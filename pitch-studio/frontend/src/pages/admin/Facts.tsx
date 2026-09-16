import DataTable from "./DataTable";

const STATUS_LABELS: Record<string, string> = {
  verified: "Verified",
  needs_source: "Needs Source",
  conflict: "Conflict",
  do_not_use: "Do Not Use",
  needs_decision: "Needs Decision",
};

export default function Facts() {
  return (
    <DataTable
      title="Locked facts"
      path="/api/admin/facts"
      createLabel="Add New Fact"
      editLabel="Edit Fact"
      fields={[
        { key: "fact", label: "Fact" },
        { key: "value", label: "Value", type: "textarea" },
        { key: "source", label: "Source" },
        {
          key: "status",
          label: "Status",
          type: "select",
          options: ["verified", "needs_source", "conflict", "do_not_use", "needs_decision"],
          optionLabels: STATUS_LABELS,
        },
        { key: "note", label: "Note", type: "textarea" },
        { key: "module_ids", label: "Module IDs" },
      ]}
    />
  );
}
