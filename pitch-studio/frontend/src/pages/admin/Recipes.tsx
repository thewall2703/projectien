import DataTable from "./DataTable";

export default function Recipes() {
  return (
    <DataTable
      title="Recipes"
      path="/api/admin/recipes"
      fields={[
        { key: "ref", label: "Ref" },
        { key: "audience_label", label: "Audience" },
        { key: "audience_cluster", label: "Cluster" },
        { key: "duration", label: "Duration" },
        { key: "channel", label: "Channel" },
        { key: "intent", label: "Intent" },
        { key: "module_sequence", label: "Modules" },
        { key: "word_budget", label: "Word budget", type: "number" },
        { key: "priority", label: "Priority", type: "select", options: ["P0", "P1", "P2"] },
        { key: "owner", label: "Owner" },
      ]}
    />
  );
}
